# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

import frappe
from frappe.utils.file_manager import save_file

from erpnext_einvoicing.erpnext_einvoicing.utils.identifiers import find_supplier


def build_purchase_invoice(epurchase_invoice):
	"""
	Builds a Purchase Invoice draft from a matched ePurchase Invoice.
	Called by ePurchaseInvoice.convert_to_purchase_invoice().
	"""
	pi = frappe.new_doc("Purchase Invoice")
	pi.einvoice_source = epurchase_invoice.name
	pi.bill_no = epurchase_invoice.invoice_number
	pi.bill_date = str(epurchase_invoice.invoice_date) if epurchase_invoice.invoice_date else None
	pi.due_date = str(epurchase_invoice.due_date) if epurchase_invoice.due_date else None
	pi.currency = epurchase_invoice.currency or "EUR"
	pi.buying_price_list = (
		frappe.db.get_single_value("Buying Settings", "buying_price_list") or "Standard Buying"
	)

	### Supplier resolution
	if epurchase_invoice.matched_supplier:
		pi.supplier = epurchase_invoice.matched_supplier
	elif epurchase_invoice.ethirdparty:
		pi.supplier = _create_supplier_from_ethirdparty(epurchase_invoice.ethirdparty)
	else:
		frappe.throw(
			frappe._("No supplier or eThirdParty linked to this ePurchase Invoice."),
			title=frappe._("Missing Supplier"),
		)

	pi.company = epurchase_invoice.company or frappe.defaults.get_user_default("Company")

	if epurchase_invoice.purchase_order:
		pi.purchase_order = epurchase_invoice.purchase_order

	if epurchase_invoice.purchase_receipt:
		pi.purchase_receipt = epurchase_invoice.purchase_receipt

	# A received credit note becomes a debit note: ERPNext expects a return
	# (is_return) with negative quantities and taxes, set before the insert.
	sign = 1
	if epurchase_invoice.is_credit_note:
		sign = -1
		pi.is_return = 1
		if epurchase_invoice.referenced_epurchase_invoice:
			pi.return_against = frappe.db.get_value(
				"ePurchase Invoice",
				epurchase_invoice.referenced_epurchase_invoice,
				"purchase_invoice",
			)

	for item in epurchase_invoice.items:
		pi.append(
			"items",
			{
				"item_code": item.matched_item,
				"item_name": item.item_description_raw,
				"qty": sign * abs(item.qty or 0),
				"uom": _resolve_uom(item.uom),
				"rate": item.unit_price,
				"amount": sign * abs(item.amount or 0),
				"purchase_order": item.purchase_order or None,
				"po_detail": item.po_detail or None,
				"purchase_receipt": item.purchase_receipt or None,
				"pr_detail": item.pr_detail or None,
				"po_match_status": item.po_match_status or None,
			},
		)

	_build_taxes(epurchase_invoice, pi, sign)

	pi.insert(ignore_permissions=True)

	epurchase_invoice.db_set("purchase_invoice", pi.name)

	attachments = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "ePurchase Invoice",
			"attached_to_name": epurchase_invoice.name,
		},
		fields=["name", "file_name", "file_url"],
	)
	for attachment in attachments:
		file_doc = frappe.get_doc("File", attachment.name)
		save_file(
			fname=attachment.file_name,
			content=file_doc.get_content(),
			dt="Purchase Invoice",
			dn=pi.name,
			is_private=1,
		)

	for item in epurchase_invoice.items:
		if not item.matched_item or not item.item_ref_raw:
			continue
		supplier_name = pi.supplier

		existing = frappe.db.get_value(
			"Item Supplier",
			{
				"parent": item.matched_item,
				"supplier": supplier_name,
			},
			"name",
		)

		if not existing:
			item_doc = frappe.get_doc("Item", item.matched_item)
			item_doc.append(
				"supplier_items",
				{
					"supplier": supplier_name,
					"supplier_part_no": item.item_ref_raw,
				},
			)
			item_doc.save(ignore_permissions=True)

	frappe.db.commit()

	return pi


### Helpers


def _resolve_uom(uom_code):
	"""Resolve a UN/CEFACT UOM code to an ERPNext UOM via eInvoicing UOM Mapping."""
	if not uom_code:
		return frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"

	mapped = frappe.db.get_value("eInvoicing UOM Mapping", uom_code, "erpnext_uom")
	if mapped and frappe.db.exists("UOM", mapped):
		return mapped

	fallback = frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"
	return fallback


def _build_taxes(epurchase_invoice, pi, sign=1):
	"""Ajoute une ligne de taxe par compte distinct basé sur tax_account_head ou lookup par taux."""
	tax_groups = {}
	for item in epurchase_invoice.items:
		if not item.tax_rate:
			continue
		rate = round(float(item.tax_rate), 1)
		account = item.get("tax_account_head") or None
		if not account:
			account = frappe.db.get_value(
				"Account",
				{
					"company": pi.company,
					"account_type": "Tax",
					"tax_rate": rate,
					"root_type": "Asset",
				},
				"name",
			)
		if not account:
			continue
		key = account
		if key not in tax_groups:
			tax_groups[key] = {"rate": rate, "base_amount": 0}
		tax_groups[key]["base_amount"] += sign * abs(float(item.amount or 0))

	for account, data in tax_groups.items():
		pi.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": account,
				"description": f"TVA {data['rate']}%",
				"tax_amount": round(data["base_amount"] * data["rate"] / 100, 2),
			},
		)


def _create_supplier_from_ethirdparty(ethirdparty_name):
	"""Creates a Supplier from an eThirdParty and returns the supplier name."""
	ethirdparty = frappe.get_doc("eThirdParty", ethirdparty_name)

	supplier = frappe.new_doc("Supplier")
	supplier.supplier_name = ethirdparty.party_name
	supplier.supplier_group = frappe.db.get_single_value(
		"Buying Settings", "supplier_group"
	) or frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
	supplier.tax_id = ethirdparty.siret
	mandatory_custom_fields = frappe.get_all(
		"Custom Field",
		filters={"dt": "Supplier", "reqd": 1},
		pluck="fieldname",
	)
	for fieldname in mandatory_custom_fields:
		value = ethirdparty.get(fieldname)
		if value:
			supplier.set(fieldname, value)

	if "erpnext_france" in frappe.get_installed_apps():
		for fieldname in ("siret", "siren", "code_naf", "legal_form"):
			value = ethirdparty.get(fieldname)
			if value:
				supplier.set(fieldname, value)

	if ethirdparty.zip:
		supplier.zip = ethirdparty.zip
	if ethirdparty.city:
		supplier.city = ethirdparty.city

	existing = find_supplier(ethirdparty.siret, ethirdparty.siren) or frappe.db.get_value(
		"Supplier", {"supplier_name": ethirdparty.party_name}, "name"
	)
	if existing:
		ethirdparty.db_set("matched_party_type", "Supplier")
		ethirdparty.db_set("matched_party", existing)
		ethirdparty.db_set("status", "converted")
		return existing

	supplier.insert(ignore_permissions=True)
	frappe.db.commit()

	ethirdparty.db_set("matched_party_type", "Supplier")
	ethirdparty.db_set("matched_party", supplier.name)
	ethirdparty.db_set("status", "converted")

	return supplier.name
