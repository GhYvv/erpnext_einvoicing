# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt
import frappe
from frappe.model.document import Document

EU_COUNTRIES = {
	"AT",
	"BE",
	"BG",
	"CY",
	"CZ",
	"DE",
	"DK",
	"EE",
	"ES",
	"FI",
	"GR",
	"HR",
	"HU",
	"IE",
	"IT",
	"LT",
	"LU",
	"LV",
	"MT",
	"NL",
	"PL",
	"PT",
	"RO",
	"SE",
	"SI",
	"SK",
}


def categorie_comptable_tiers(country_code):
	"""The erpnext_france third-party category for a country, if it exists on the site."""
	if not country_code:
		return None
	# table_exists() adds the "tab" prefix itself
	doctype = (
		"Categorie Comptable Tiers"
		if frappe.db.table_exists("Categorie Comptable Tiers")
		else "Categorie comptable Tiers"
	)
	if not frappe.db.table_exists(doctype):
		return None
	code = country_code.upper()
	if code == "FR":
		category = "France"
	elif code in EU_COUNTRIES:
		category = "UE"
	else:
		category = "Export"
	return category if frappe.db.exists(doctype, category) else None


class eThirdParty(Document):
	def before_save(self):
		# The category now lives on the Supplier only (99e245e removed the field here)
		if self.meta.has_field("categorie_comptable_tiers") and not self.categorie_comptable_tiers:
			self.categorie_comptable_tiers = categorie_comptable_tiers(self.country_code)
