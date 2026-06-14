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


def print_monthly_overview(monthly_statement):
	print(f"Total sales in month:{monthly_statement["total_gross"]} \n")
	print(f"Total discounts in month: {monthly_statement["total_discounts"]}\n")
	print(f"Total refunds in month: {monthly_statement["total_refunds"]}\n")
	print("---------------------")
	print(f"leaving a total of {monthly_statement["sales_after_refunds_and_discounts"]} in sales after refunds and discounts")



def fetch_monhtly_statement(start, end, key):

	monthly_statement = {
		"sales_after_refunds_and_discounts": 0,
	    "total_gross": 0,
	    "total_taxes": 0,
	    "total_discounts": 0,
	    "total_refunds": 0,
	    "all_invoices": [], 
	    "all_refunds": [],
	}


	raw_invoice_data = list(
		client.v1.invoices.list({
			"limit": 100, 
			"created": {
				"gte": int(start.timestamp()), 
				"lte": int(end.timestamp()),
			}, 
		}).auto_paging_iter() 
	)



	#Fetch invoices
	for invoice in raw_invoice_data: 

		discount_list = getattr(invoice, 'total_discount_amounts', []) or []
		discount_sum = sum(d.amount for d in discount_list)


		tax_list = getattr(invoice, 'total_taxes', []) or []
		tax_sum = sum(t.amount for t in tax_list)

		date = getattr(invoice, 'status_transitions')["paid_at"]

		monthly_statement["all_invoices"].append({
			"id": invoice.id, 
			"date": date, #unix-format
			"status": invoice.status,
			"gross_amount": invoice.total,
			"discount_amount": discount_sum, 
			"tax": tax_sum
			})

		monthly_statement["total_gross"] += invoice.total
		monthly_statement["total_taxes"] += tax_sum
		monthly_statement["total_discounts"] += discount_sum


	# chceck paid invoices only
	for invoice in monthly_statement["all_invoices"]: 
		if invoice["status"] != "paid": 
			print(f"Obs! not all invoices listed have status paid")
			print(invoice)


	#Fetch refunds
	raw_refund_data = list(
		client.v1.refunds.list({
			"limit": 100,
			"created": {
				"gte": int(start.timestamp()),
				"lte": int(end.timestamp()),
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


	# Calculate sales after refunds and discounts
	monthly_statement["sales_after_refunds_and_discounts"] = monthly_statement["total_gross"] - monthly_statement["total_discounts"] - monthly_statement["total_refunds"]

	return monthly_statement

	#Check and correct VAT

#Displaying
# print_monthly_overview(monthly_statement)


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


	# with open(report_run.result.filename, "wb") as f: 
	# 	f.write(resp.content)

	# print(f"Saved {report_run.result.filename} ({len(resp.content)} bytes)"

	monthly_fee_statement = defaultdict(int)


	for row in rows:
		monthly_fee_statement[row["product"]] += int(Decimal(row["amount"]) * 100)

		monthly_fee_statement["totals"] += int(Decimal(row["amount"]) * 100) # ska sedan avrundas till jämt 50 öre

	print(dict(monthly_fee_statement)) 

# def readstripe_fee_report(): 

### kör programmet:

# may = fetch_monhtly_statement(start, end, stripe_key)
# print_monthly_overview(may)

fetch_stripe_monthly_fees(start, end, stripe_key)



# export monthly statements to pdf
# export stripe fees as pdf



