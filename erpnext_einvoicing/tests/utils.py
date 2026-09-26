# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

"""Test fixtures.

The tests build their own data instead of relying on ERPNext's global test
records: those break as soon as erpnext_france is installed, because it makes
custom fields such as `Supplier.categorie_comptable_tiers` mandatory.
"""

import frappe
from frappe.utils.data import now_datetime

TEST_COMPANY = "eInvoicing Test SAS"
CHART = "France - Plan Comptable General 2025 avec code"


def ensure_test_company():
	"""A French company with the 2025 French chart of accounts."""
	if not frappe.db.a_row_exists("Company"):
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		year = now_datetime().year
		setup_complete(
			{
				"currency": "EUR",
				"full_name": "Test User",
				"company_name": TEST_COMPANY,
				"company_abbr": "EIT",
				"timezone": "Europe/Paris",
				"country": "France",
				"fy_start_date": f"{year}-01-01",
				"fy_end_date": f"{year}-12-31",
				"language": "english",
				"email": "test@example.com",
				"password": "test",
				"chart_of_accounts": CHART,
			}
		)
	company = frappe.db.get_value("Company", {"country": "France"}, "name", order_by="creation asc")
	if not company:
		company = (
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": TEST_COMPANY,
					"abbr": "EIT",
					"country": "France",
					"default_currency": "EUR",
					"create_chart_of_accounts_based_on": "Standard Template",
					"chart_of_accounts": CHART,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	_ensure_fiscal_year(company)
	if not frappe.db.get_value("Company", company, "round_off_account"):
		frappe.db.set_value(
			"Company",
			company,
			"round_off_account",
			frappe.db.get_value(
				"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
			),
		)
	frappe.db.commit()
	return company


def other_company(name="eInvoicing Other SAS", abbr="EIO"):
	"""A second French company, for multi-company cases."""
	if not frappe.db.exists("Company", name):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": name,
				"abbr": abbr,
				"country": "France",
				"default_currency": "EUR",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": CHART,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	return name


def _ensure_fiscal_year(company):
	year = now_datetime().year
	start, end = f"{year}-01-01", f"{year}-12-31"
	fiscal_year = frappe.db.get_value("Fiscal Year", {"year_start_date": start, "year_end_date": end}, "name")
	if not fiscal_year:
		frappe.get_doc(
			{"doctype": "Fiscal Year", "year": str(year), "year_start_date": start, "year_end_date": end}
		).insert(ignore_permissions=True)
		return
	fy = frappe.get_doc("Fiscal Year", fiscal_year)
	if fy.companies and company not in [row.company for row in fy.companies]:
		fy.append("companies", {"company": company})
		fy.save(ignore_permissions=True)


def _fill_mandatory_custom_links(doc):
	"""Give mandatory custom Link fields (added by other apps) an existing value."""
	for field in frappe.get_all(
		"Custom Field",
		filters={"dt": doc.doctype, "reqd": 1, "fieldtype": "Link"},
		fields=["fieldname", "options"],
	):
		if doc.get(field.fieldname):
			continue
		value = frappe.db.get_value(field.options, {}, "name")
		if not value:
			autoname = frappe.get_meta(field.options).autoname or ""
			if autoname.startswith("field:"):
				value = (
					frappe.get_doc({"doctype": field.options, autoname[6:]: "_Test"})
					.insert(ignore_permissions=True)
					.name
				)
		doc.set(field.fieldname, value)


def tax_account(company, rate):
	account = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Tax", "tax_rate": rate, "is_group": 0},
		"name",
	)
	if account:
		return account
	parent = frappe.db.get_value("Account", {"company": company, "root_type": "Asset", "is_group": 1}, "name")
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": f"TVA deductible {rate}",
				"company": company,
				"parent_account": parent,
				"account_type": "Tax",
				"tax_rate": rate,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def supplier(name="_Test eInvoicing Supplier"):
	if not frappe.db.exists("Supplier", name):
		doc = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": name,
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
			}
		)
		_fill_mandatory_custom_links(doc)
		doc.insert(ignore_permissions=True)
	return name


def item(company, code="_Test eInvoicing Service"):
	if frappe.db.exists("Item", code):
		doc = frappe.get_doc("Item", code)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		)
	row = next((d for d in doc.item_defaults if d.company == company), None) or doc.append(
		"item_defaults", {"company": company}
	)
	if not row.expense_account:
		row.expense_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
		)
	doc.save(ignore_permissions=True)
	return code


def make_epurchase_invoice(company, is_credit_note=0, referenced=None, qty=2, unit_price=50, tax_rate=20):
	return frappe.get_doc(
		{
			"doctype": "ePurchase Invoice",
			"approved_platform": "SUPER PDP",
			"company": company,
			"invoice_number": frappe.generate_hash(length=10),
			"invoice_date": "2026-09-01",
			"currency": "EUR",
			"supplier_match_status": "matched",
			"matched_supplier": supplier(),
			"is_credit_note": is_credit_note,
			"referenced_epurchase_invoice": referenced,
			"items": [
				{
					"item_description_raw": "Service",
					"qty": qty,
					"unit_price": unit_price,
					"amount": qty * unit_price,
					"tax_rate": tax_rate,
					"tax_account_head": tax_account(company, tax_rate),
					"match_status": "matched",
					"matched_item": item(company),
				}
			],
		}
	).insert(ignore_permissions=True)


def cii_invoice(
	buyer_id,
	seller_siret,
	seller_siren="",
	seller_vat="",
	seller_name="Fournisseur SAS",
	number=None,
	type_code="380",
):
	"""A minimal Factur-X (CII) invoice, as a received flow carries it."""
	number = number or frappe.generate_hash(length=10)
	siren = (
		f'<ram:SpecifiedLegalOrganization><ram:ID schemeID="0002">{seller_siren}</ram:ID>'
		"</ram:SpecifiedLegalOrganization>"
		if seller_siren
		else ""
	)
	vat = (
		f'<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{seller_vat}</ram:ID>'
		"</ram:SpecifiedTaxRegistration>"
		if seller_vat
		else ""
	)
	return f"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
	xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
	xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
	xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
<rsm:ExchangedDocument>
	<ram:ID>{number}</ram:ID><ram:TypeCode>{type_code}</ram:TypeCode>
	<ram:IssueDateTime><udt:DateTimeString format="102">20260901</udt:DateTimeString></ram:IssueDateTime>
</rsm:ExchangedDocument>
<rsm:SupplyChainTradeTransaction>
	<ram:IncludedSupplyChainTradeLineItem>
		<ram:SpecifiedTradeProduct><ram:Name>Service</ram:Name></ram:SpecifiedTradeProduct>
		<ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice>
			<ram:ChargeAmount>100.00</ram:ChargeAmount>
		</ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>
		<ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="C62">1</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
		<ram:SpecifiedLineTradeSettlement>
			<ram:ApplicableTradeTax><ram:RateApplicablePercent>20</ram:RateApplicablePercent></ram:ApplicableTradeTax>
			<ram:SpecifiedTradeSettlementLineMonetarySummation>
				<ram:LineTotalAmount>100.00</ram:LineTotalAmount>
			</ram:SpecifiedTradeSettlementLineMonetarySummation>
		</ram:SpecifiedLineTradeSettlement>
	</ram:IncludedSupplyChainTradeLineItem>
	<ram:ApplicableHeaderTradeAgreement>
		<ram:SellerTradeParty>
			<ram:GlobalID schemeID="0009">{seller_siret}</ram:GlobalID>
			<ram:Name>{seller_name}</ram:Name>{siren}{vat}
		</ram:SellerTradeParty>
		<ram:BuyerTradeParty>
			<ram:Name>Acheteur</ram:Name>
			<ram:SpecifiedLegalOrganization><ram:ID schemeID="0002">{buyer_id}</ram:ID></ram:SpecifiedLegalOrganization>
		</ram:BuyerTradeParty>
	</ram:ApplicableHeaderTradeAgreement>
	<ram:ApplicableHeaderTradeSettlement>
		<ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
		<ram:SpecifiedTradeSettlementHeaderMonetarySummation>
			<ram:TaxBasisTotalAmount>100.00</ram:TaxBasisTotalAmount>
			<ram:TaxTotalAmount currencyID="EUR">20.00</ram:TaxTotalAmount>
			<ram:GrandTotalAmount>120.00</ram:GrandTotalAmount>
		</ram:SpecifiedTradeSettlementHeaderMonetarySummation>
	</ram:ApplicableHeaderTradeSettlement>
</rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""
