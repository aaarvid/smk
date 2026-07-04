from collections import defaultdict
from stripe import StripeClient
from datetime import timedelta
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
import requests
import time
import sys
import csv
import io

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
	SimpleDocTemplate, LongTable, TableStyle, Paragraph, Spacer
)
from reportlab.lib.styles import getSampleStyleSheet

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


def to_printable_sek(ore: int) -> str: 
	# in 1074250
	#ut: 10 742, 50

	#runda nedåt till 0 eller 50 öre
	ore = (ore // 50) * 50

	kr, rest = divmod(int(ore), 100)

	# The :02d pads rest to two digits, so 5 öre shows as 05 not 5.
	return f"{kr}:{rest:02d} SEK"

	# ore_str = str(ore)
	# sek_str = ore_str[0:(len(ore_str)-2)] + "." + ore_str[(len(ore_str)-2):len(ore_str)] + " " + "SEK"

	## runda till närmaste 50 öre

	# return sek_str


#Displaying
def print_monthly_overview(monthly_statement, monthly_fee_statement=None, to_deposit=None):
	print("---------------------")
	print(f"{len(monthly_statement["all_invoices"])} of subscriptions sold")
	print("---------------------")
	print(f"Total sales in month:{to_printable_sek(monthly_statement["total_gross"])}")
	print(f"Total discounts in month: {to_printable_sek(monthly_statement["total_discounts"])}")
	print(f"A total of {len(monthly_statement["all_refunds"])} refunds where made in the preiod, summing up to: {to_printable_sek(monthly_statement["total_refunds"])}")
	print("---------------------")
	print(f"There are {len(monthly_statement["unpaid_invoices"])} of unpaid invoices during the period")
	print("These are exempt from sums and calculations")
	print("---------------------")
	print(f"leaving a total of {to_printable_sek(monthly_statement["sales_after_refunds_and_discounts"])} in sales after refunds and discounts")
	print("---------------------")
	if monthly_statement["total_corrected_taxes"] != 0:
		print(f"Total Tax in month: {to_printable_sek(monthly_statement["total_corrected_taxes"])}")
		print(f"At a ratio of {(monthly_statement["total_corrected_taxes"] / monthly_statement["sales_after_refunds_and_discounts"])* 100 } %")
		print(f"This after a correction of {to_printable_sek(monthly_statement["tax_adjustment"])}")

	else:
		print(f"Total Tax in month: {to_printable_sek(monthly_statement["total_reported_taxes"])}")
		print(f"At a ratio of {(monthly_statement["total_reported_taxes"] / monthly_statement["sales_after_refunds_and_discounts"])* 100 } %")
	print("---------------------")

	if monthly_fee_statement:
		print("---------------------")
		print(f"Stripe fees this month:")
		for product, amount in monthly_fee_statement.items():
			print(f"{product}: {to_printable_sek(amount)}")
		print("---------------------")
	if to_deposit:
		print("---------------------")
		print(f"To deposit to company account: {to_printable_sek(to_deposit)}")
		print("---------------------")



def fetch_monhtly_statement(start, end):

	monthly_statement = {
		"total_gross": 0,
		"total_reported_taxes": 0,
		"total_corrected_taxes": 0,
		"tax_adjustment": 0,
		"total_discounts": 0,
		"total_refunds": 0,
		"sales_after_refunds_and_discounts": 0,
		"amount_to_deposit": 0,
		"all_invoices": [], 
		"all_refunds": [],
		"unpaid_invoices": [],
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

		if invoice.status != "paid":
			print(f"Obs! Not all invoices whithin period have status paid. Skipping.")
			print(f"ID:{invoice.id}, date: {invoice.created}, amount:{invoice.total}")

			monthly_statement["unpaid_invoices"].append({
				"id": invoice.id,
				"date": invoice.created, #unix-format
				"status": invoice.status,
				"gross_amount": invoice.total, #discounts redan bortdragna
				"discount_amount": discount_sum,
				"tax": tax_sum
				})

			continue


		monthly_statement["all_invoices"].append({
			"id": invoice.id,
			"date": invoice.created, #unix-format
			"status": invoice.status,
			"gross_amount": invoice.total, #discounts redan bortdragna
			"discount_amount": discount_sum,
			"tax": tax_sum
			})

		monthly_statement["total_gross"] += invoice.total
		monthly_statement["total_reported_taxes"] += tax_sum
		monthly_statement["total_discounts"] += discount_sum


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


def check_vat(month):
	if 5 * month["total_reported_taxes"] == month["sales_after_refunds_and_discounts"]:
		print("---------------------")
		print("tax is correct at 20%")
	else:
		tax_ratio = month["total_reported_taxes"] / month["sales_after_refunds_and_discounts"]
		print("---------------------")
		print(f"Current tax ratio is {tax_ratio}, total tax: {month["total_reported_taxes"]} of sales (af r & d) {month["sales_after_refunds_and_discounts"]}")
		month["total_corrected_taxes"] = month["sales_after_refunds_and_discounts"] // 5 # // discard remainder
		month["tax_adjustment"] = month["total_corrected_taxes"] - month["total_reported_taxes"]
		print(f"Updated tax: {to_printable_sek(month["total_reported_taxes"])} -> {to_printable_sek(month["total_corrected_taxes"])}")
		print(f"Now at a ratio of {month["total_corrected_taxes"] / month["sales_after_refunds_and_discounts"]}")


def fetch_stripe_monthly_fees(start, end):
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
		# monthly_fee_statement["total_fees"] += int(Decimal(row["amount"]) * 100) # ska sedan avrundas till jämt 50 öre

	monthly_fee_statement["total_fees"] = sum(monthly_fee_statement.values())
	return monthly_fee_statement

# Calculate what to deposit from Stripe to company account
def to_deposit(monthly_statement, monthly_fee_statement):
	return monthly_statement["sales_after_refunds_and_discounts"] - monthly_fee_statement["total_fees"]


# Stuff for generating pdfs
def _page_footer(canvas, doc):
	canvas.saveState()
	canvas.setFont("Helvetica", 8)
	w, _ = landscape(A4)
	canvas.drawRightString(w - 10 * mm, 6 * mm, f"Sida {doc.page}")
	canvas.restoreState()


_styles = getSampleStyleSheet()
_cell = _styles["BodyText"]
_cell.fontSize = 6
_cell.leading = 7


def _make_table(headers, rows, col_widths=None):
	head = [Paragraph(f'<font color="white"><b>{h}</b></font>', _cell) for h in headers]
	body = [[Paragraph(str(c), _cell) for c in r] for r in rows]
	t = LongTable([head] + body, colWidths=col_widths, repeatRows=1)
	t.setStyle(TableStyle([
		("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0048a3")),
		("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
		("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
		("ROWBACKGROUNDS", (0, 1), (-1, -1),
		[colors.white, colors.HexColor("#f2f2f2")]),
		("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
		("LEFTPADDING", (0, 0), (-1, -1), 2),
		("RIGHTPADDING", (0, 0), (-1, -1), 2),
		("TOPPADDING", (0, 0), (-1, -1), 1),
		("BOTTOMPADDING", (0, 0), (-1, -1), 1),
	]))
	return t


def build_statement(path, title, statement):
	doc = SimpleDocTemplate(
		path,
		pagesize=landscape(A4),
		leftMargin=10 * mm, rightMargin=10 * mm,
		topMargin=12 * mm, bottomMargin=14 * mm,
	)

	styles = getSampleStyleSheet()
	cell = styles["BodyText"]
	cell.fontSize = 6
	cell.leading = 7


	# --- Summary block ---
	summary_rows = [
		["Totalt brutto", to_printable_sek(statement["total_gross"])],
		["Total automatiskt uträknad moms", to_printable_sek(statement["total_reported_taxes"])],
		["Total moms", to_printable_sek(statement["total_corrected_taxes"])],
		["Justerad moms", to_printable_sek(statement["tax_adjustment"])],
		["Totala rabatter", to_printable_sek(statement["total_discounts"])],
		["Total återbetalningar", to_printable_sek(statement["total_refunds"])],
		["Försäljning efter återbetalningar och rabatter",
		to_printable_sek(statement["sales_after_refunds_and_discounts"])],
	]
	summary = LongTable(
		[[Paragraph(f"<b>{k}</b>", cell), Paragraph(v, cell)]
		for k, v in summary_rows],
		colWidths=[70 * mm, 30 * mm],
	)
	summary.setStyle(TableStyle([
		("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
		("ALIGN", (1, 0), (1, -1), "RIGHT"),
		("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
		("LEFTPADDING", (0, 0), (-1, -1), 3),
		("RIGHTPADDING", (0, 0), (-1, -1), 3),
		("TOPPADDING", (0, 0), (-1, -1), 2),
		("BOTTOMPADDING", (0, 0), (-1, -1), 2),
	]))

	# --- Invoice rows: match the keys produced by fetch_monhtly_statement ---
	inv_headers = ["Id", "Datum", "Status", "Brutto", "Rabatt", "Moms"]
	inv_rows = [[
		inv.get("id", ""),
		datetime.fromtimestamp(inv.get("date", 0), tz).strftime("%Y-%m-%d %H:%M"),
		inv.get("status", ""),
		to_printable_sek(inv.get("gross_amount", 0)),
		to_printable_sek(inv.get("discount_amount", 0)),
		to_printable_sek(inv.get("tax", 0)),
	] for inv in statement["all_invoices"]]

	ref_headers = ["Återbetalnings-ID", "Datum", "Summa"]
	ref_rows = [[
		r.get("refund_id", ""),
		datetime.fromtimestamp(r.get("refund_date", 0), tz).strftime("%Y-%m-%d %H:%M"),
		to_printable_sek(r.get("refund_amount", 0)),
	] for r in statement["all_refunds"]]

	h1 = styles["Heading2"]
	elements = [
		Paragraph(title, styles["Title"]),
		Spacer(1, 4 * mm),
		summary,
		Spacer(1, 6 * mm),
		Paragraph("Fakturor", h1),
		_make_table(inv_headers, inv_rows),
	]
	if ref_rows:
		elements += [
			Spacer(1, 6 * mm),
			Paragraph("Återbetalningar", h1),
			_make_table(ref_headers, ref_rows),
		]

	doc.build(elements, onFirstPage=_page_footer, onLaterPages=_page_footer)


def build_fee_statement(path, title, fee_statement):
	doc = SimpleDocTemplate(
		path,
		pagesize=landscape(A4),
		leftMargin=10 * mm, rightMargin=10 * mm,
		topMargin=12 * mm, bottomMargin=14 * mm,
	)

	# Per-product rows, excluding the total; sorted biggest first
	fee_headers = ["Produkt", "Avgift"]
	fee_rows = [
		[product, to_printable_sek(amount)]
		for product, amount in sorted(
			fee_statement.items(),
			key=lambda kv: kv[1],
			reverse=True,
		)
		if product != "total_fees"
	]
	fee_rows.append(["Totala avgifter", to_printable_sek(fee_statement["total_fees"])])

	elements = [
		Paragraph(title, _styles["Title"]),
		Spacer(1, 4 * mm),
		_make_table(fee_headers, fee_rows, col_widths=[120 * mm, 40 * mm]),
	]
	doc.build(elements, onFirstPage=_page_footer, onLaterPages=_page_footer)

# kör programmet

def main(): 
	if len(sys.argv) < 3:
		print("kör programmet med Python3 main.py yyyy-mm-dd yyyy-mm-dd")
		print("Startdatum är inklusivt, slutdatum exklusivt, sätt sluttdatum till ex.vis 1 juni för att få med hela maj månad")
		sys.exit(1)

	# start = datetime(2026, 5, 1, 0, 0, tzinfo=tz)
	# end = datetime(2026,6, 1, 0, 0, tzinfo=tz) includes all of may, but none of june
	#2026-05-01 00:00:00+02:00

	try:
		start = datetime.strptime(sys.argv[1].lstrip("-"), "%Y-%m-%d").replace(tzinfo=tz)
	except Exception as e:
		print(f"Fel format i startdatum. Ska vara yyyy-mm-dd. Var: {sys.argv[1]}")
		sys.exit(1)
	try:
		end = datetime.strptime(sys.argv[2].lstrip("-"), "%Y-%m-%d").replace(tzinfo=tz)
	except Exception as e:
		print(f"Fel format i startdatum. Ska vara yyyy-mm-dd. Var: {sys.argv[2]}")
		sys.exit(1)

	print(f"Start: {start}")
	print(f"End: {end}")


	try:
		monthly_statement = fetch_monhtly_statement(start, end)	
	except Exception as e: 
		print(f"Error fetching monthly statement, {e}")

	monthly_fees = fetch_stripe_monthly_fees(start, end)

	check_vat(monthly_statement)

	may_deposit = to_deposit(monthly_statement, monthly_fees)

	print_monthly_overview(monthly_statement, monthly_fees, may_deposit)



	### generate verifications

	print(f"Generate pdf verifications? (y/n)")
	should_generate = input().lower()

	if should_generate == "y":

		statement_filename=f"SMK_{start.strftime('%Y%m%d')}-{(end - timedelta(days=1)).strftime('%Y%m%d')}_sammanställning.pdf"
		statement_title=f"Svenska Mjukvarukontoret Försäljningssammanställning {start.strftime('%Y%m%d')}-{(end - timedelta(days=1)).strftime('%Y%m%d')}"


		build_statement(statement_filename, statement_title, monthly_statement)
		print("PDF written with monthly statement")

		fee_filename=f"SMK_{start.strftime('%Y%m%d')}-{(end - timedelta(days=1)).strftime('%Y%m%d')}_stripeavgifter.pdf"
		fee_title=f"Svenska Mjukvarukontoret Stripe-avgifter {start.strftime('%Y%m%d')}-{(end - timedelta(days=1)).strftime('%Y%m%d')}"

		build_fee_statement(fee_filename, fee_title, monthly_fees)
		print("PDF written with stripe fees")
	else:
		sys.exit(1)


if __name__ == "__main__":
    main()


