# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

"""Companies and suppliers are matched on their SIRET/SIREN fields.

erpnext_france keeps the intra-community VAT number in `tax_id` and the
SIRET/SIREN in dedicated fields: matching on `tax_id == SIRET` finds nothing.
"""

import unittest

import frappe

from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import create_e_purchase_invoice_from_xml
from erpnext_einvoicing.tests.utils import cii_invoice, ensure_test_company, other_company, supplier

COMPANY_SIRET = "12345678900011"
COMPANY_VAT = "FR12123456789"
SUPPLIER_SIRET = "98765432100027"
SUPPLIER_VAT = "FR45987654321"


class TestIdentifiers(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.get_meta("Company").has_field("siret"):
			raise unittest.SkipTest("SIRET fields come with erpnext_france")
		cls.company = ensure_test_company()
		frappe.db.set_value(
			"Company",
			cls.company,
			{"siret": COMPANY_SIRET, "tax_id": COMPANY_VAT, "einvoicing_approved_platform": "SUPER PDP"},
		)
		cls.supplier = supplier("_Test SIRET Supplier")
		frappe.db.set_value(
			"Supplier",
			cls.supplier,
			{"siret": SUPPLIER_SIRET, "siren": SUPPLIER_SIRET[:9], "tax_id": SUPPLIER_VAT},
		)
		frappe.db.commit()

	def setUp(self):
		# The user's default company must not be what finds the buyer.
		self.default_company = frappe.defaults.get_user_default("company")
		frappe.defaults.set_user_default("company", other_company())

	def tearDown(self):
		frappe.defaults.set_user_default("company", self.default_company)

	def receive(self, **kwargs):
		kwargs.setdefault("seller_name", "Unknown name")
		return create_e_purchase_invoice_from_xml(cii_invoice(**kwargs), {"flowId": frappe.generate_hash()})

	def test_buyer_siret_finds_the_company(self):
		einv = self.receive(buyer_id=COMPANY_SIRET, seller_siret=SUPPLIER_SIRET)
		self.assertEqual(einv.company, self.company)

	def test_buyer_siren_finds_the_company(self):
		einv = self.receive(buyer_id=COMPANY_SIRET[:9], seller_siret=SUPPLIER_SIRET)
		self.assertEqual(einv.company, self.company)

	def test_seller_siret_finds_the_supplier(self):
		einv = self.receive(buyer_id=COMPANY_SIRET, seller_siret=SUPPLIER_SIRET)
		einv.reload()
		self.assertEqual(einv.matched_supplier, self.supplier)

	def test_seller_siren_finds_the_supplier(self):
		einv = self.receive(buyer_id=COMPANY_SIRET, seller_siret="", seller_siren=SUPPLIER_SIRET[:9])
		einv.reload()
		self.assertEqual(einv.matched_supplier, self.supplier)
