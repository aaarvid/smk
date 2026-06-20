from stripe import StripeClient
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from decimal import Decimal
import requests
import time
import csv
import io



def get_env_value(path, key):
	with open(path) as f:
		for line in f:
			if not line or line.startswith("#"):
				continue
			line = line.strip()
			if line.startswith(key + "="):
				return line.split("=", 1)[1]
	return None

stripe_key = get_env_value(".env", "STRIPE_API_KEY")
client = StripeClient(stripe_key)

tz = ZoneInfo("Europe/Stockholm")

start = datetime(2026, 5, 1, 0, 0, tzinfo=tz)
end = datetime(2026,6, 1, 0, 0, tzinfo=tz)

def to_printable_sek(ore: int) -> str: 
	# in 1074250
	#ut: 10 742, 50
	ore_str = str(ore)

	# lägga in mellanrum för tusental
	sek_str = ore_str[0:(len(ore_str)-2)] + "." + ore_str[(len(ore_str)-2):len(ore_str)] + " " + "SEK"

	## runda till närmaste 50 öre

	return sek_str


#Displaying
def print_monthly_overview(monthly_statement, monthly_fee_statement=None, to_deposit=None):
	print("---------------------")
	print(f"Total sales in month:{to_printable_sek(monthly_statement["total_gross"])}")
	print(f"Total discounts in month: {to_printable_sek(monthly_statement["total_discounts"])}")
	print(f"Total refunds in month: {to_printable_sek(monthly_statement["total_refunds"])}")
	print("---------------------")
	print(f"leaving a total of {to_printable_sek(monthly_statement["sales_after_refunds_and_discounts"])} in sales after refunds and discounts")
	print("---------------------")
	print(f"Total Tax in month: {to_printable_sek(monthly_statement["total_taxes"])}")
	print(f"At a ratio of {(monthly_statement["total_taxes"] / monthly_statement["sales_after_refunds_and_discounts"])* 100 } %")
	print("---------------------")

	if monthly_fee_statement:
		print(f"Stripe fees this month:")
		for product, amount in monthly_fee_statement.items():
			print(f"{product}: {amount}")

		print(f"total fees: {monthly_fee_statement["total_fees"]}")

	if to_deposit:
		print(f"To deposit to company account: {to_deposit}")



def fetch_monhtly_statement(start, end, key):

	monthly_statement = {
	    "total_gross": 0,
	    "total_taxes": 0,
	    "total_discounts": 0,
	    "total_refunds": 0,
		"sales_after_refunds_and_discounts": 0,
		"amount_to_deposit": 0,
	    "all_invoices": [], 
	    "all_refunds": [],
	}


	raw_invoice_data = list(
		client.v1.invoices.list({
			"limit": 100, 
			"created": {
				"gte": int(start.timestamp()), 
				"lt": int(end.timestamp()),
			}, 
		}).auto_paging_iter() 
	)



	#Fetch invoices
	for invoice in raw_invoice_data: 

		discount_list = getattr(invoice, 'total_discount_amounts', []) or []
		discount_sum = sum(d.amount for d in discount_list)


		tax_list = getattr(invoice, 'total_taxes', []) or []
		tax_sum = sum(t.amount for t in tax_list)

		# date = getattr(invoice, 'status_transitions')["paid_at"]

		monthly_statement["all_invoices"].append({
			"id": invoice.id, 
			"date": invoice.created, #unix-format
			"status": invoice.status,
			"gross_amount": invoice.total, #discounts redan bortdragna
			"discount_amount": discount_sum, 
			"tax": tax_sum
			})

		monthly_statement["total_gross"] += invoice.total
		monthly_statement["total_taxes"] += tax_sum
		monthly_statement["total_discounts"] += discount_sum


	# chceck paid invoices only
	for invoice in monthly_statement["all_invoices"]: 
		if invoice["status"] != "paid": 
			print(f"Obs! Not all invoices listed have status paid: investigate")
			print(invoice)


	#Fetch refunds
	raw_refund_data = list(
		client.v1.refunds.list({
			"limit": 100,
			"created": {
				"gte": int(start.timestamp()),
				"lt": int(end.timestamp()),
			},
		}).auto_paging_iter()
	)

	for refund in raw_refund_data:
		monthly_statement["all_refunds"].append({
			"refund_id": refund.id,
			"refund_date": refund.created, 
			"refund_amount": refund.amount
			})

		monthly_statement["total_refunds"] += refund.amount

	# Calculate sales after refunds and discounts (total_gross is already without discounts)
	monthly_statement["sales_after_refunds_and_discounts"] = monthly_statement["total_gross"] - monthly_statement["total_refunds"]

	return monthly_statement

	#Check and correct VAT


def check_vat(monthly_statement):
	if 5 * monthly_statement["total_taxes"] == monthly_statement["sales_after_refunds_and_discounts"]:
		print("tax is correct at 20%")
	else:
		tax_ratio = monthly_statement["total_taxes"] / monthly_statement["sales_after_refunds_and_discounts"]
		print(f"Current tax ratio is {tax_ratio}, total tax: {monthly_statement["total_taxes"]} of sales (af r & d) {monthly_statement["sales_after_refunds_and_discounts"]}")
		updated_tax = monthly_statement["sales_after_refunds_and_discounts"] / 5
		print(f"Updated tax: {monthly_statement["total_taxes"]} -> {updated_tax}")
		print(f"Now at a ration of {updated_tax / monthly_statement["sales_after_refunds_and_discounts"]}")
		monthly_statement["total_taxes"] = updated_tax


def fetch_stripe_monthly_fees(start, end, key):
	report_run = client.v1.reporting.report_runs.create({
		"report_type": "all_fees.balance_transaction_created.summary.2",
		"parameters": {
			"interval_start": int(start.timestamp()),
			"interval_end": int(end.timestamp()),  # exclusive
			"currency": "sek",
		},
	})

	while report_run.status == "pending":
		time.sleep(2)
		report_run = client.v1.reporting.report_runs.retrieve(report_run.id)

	if report_run.status != "succeeded": 
		raise RuntimeError(f"Report failed")


	report_url = report_run.result.url

	resp = requests.get(report_url, auth=(stripe_key, ""))
	resp.raise_for_status() #raises http error


	reader = csv.DictReader(io.StringIO(resp.text))
	rows = list(reader)

	monthly_fee_statement = defaultdict(int)

	for row in rows:
		monthly_fee_statement[row["product"]] += int(Decimal(row["amount"]) * 100)
		monthly_fee_statement["total_fees"] += int(Decimal(row["amount"]) * 100) # ska sedan avrundas till jämt 50 öre

	return monthly_fee_statement


# Calculate what to deposit from Stripe to company account
def to_deposit(monthly_statement, monthly_fee_statement):
	return monthly_statement["sales_after_refunds_and_discounts"] - monthly_fee_statement["total_fees"]


### kör programmet:

try: 
	may = fetch_monhtly_statement(start, end, stripe_key)	
except Exception as e: 
	print(f"Error fetching monthly statement, {e}")

may_fees = fetch_stripe_monthly_fees(start, end, stripe_key)

check_vat(may)

may_deposit = to_deposit(may, may_fees)

print_monthly_overview(may, may_fees, may_deposit)




# generate verifications

# export monthly statements to pdf
# export stripe fees as pdf



