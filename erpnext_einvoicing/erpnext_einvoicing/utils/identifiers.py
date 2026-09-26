# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

"""Find companies and suppliers from the SIRET/SIREN carried by an invoice.

erpnext_france stores SIRET and SIREN in dedicated fields and keeps the
intra-community VAT number in `tax_id`. Without erpnext_france, the
identifiers are looked up in `tax_id`, as before.
"""

import frappe


def _clean(value):
	return (value or "").replace(" ", "")


def find_company(identifier):
	"""Company from the buyer's SIRET (14 digits) or SIREN (9 digits)."""
	identifier = _clean(identifier)
	if not identifier:
		return None
	if frappe.get_meta("Company").has_field("siret"):
		company = frappe.db.get_value("Company", {"siret": identifier}, "name")
		if not company and len(identifier) == 9:
			company = frappe.db.get_value("Company", {"siret": ["like", f"{identifier}%"]}, "name")
		if company:
			return company
	return frappe.db.get_value("Company", {"tax_id": identifier}, "name")


def find_supplier(siret="", siren=""):
	"""Supplier from the seller's SIRET, then from its SIREN (same legal entity)."""
	siret = _clean(siret)
	siren = _clean(siren) or siret[:9]
	meta = frappe.get_meta("Supplier")
	for fieldname, value in (("siret", siret), ("siren", siren)):
		if value and meta.has_field(fieldname):
			supplier = frappe.db.get_value("Supplier", {fieldname: value}, "name")
			if supplier:
				return supplier
	for value in (siret, siren):
		if value:
			supplier = frappe.db.get_value("Supplier", {"tax_id": value}, "name")
			if supplier:
				return supplier
	return None
