# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

"""The purchase invoice carries the VAT stated on the received invoice.

The supplier's invoice is the document that grounds the VAT deduction: the
conversion must not recompute it, nor drop a rate without a tax account.
"""

import unittest

import frappe

from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import create_e_purchase_invoice_from_xml
from erpnext_einvoicing.tests import test_identifiers
from erpnext_einvoicing.tests.utils import cii_invoice, item, tax_account


class TestConversionVat(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		test_identifiers.TestIdentifiers.setUpClass()
		cls.company = test_identifiers.TestIdentifiers.company

	def convert(self, **kwargs):
		einv = create_e_purchase_invoice_from_xml(
			cii_invoice(
				buyer_id=test_identifiers.COMPANY_SIRET,
				seller_siret=test_identifiers.SUPPLIER_SIRET,
				**kwargs,
			),
			{"flowId": frappe.generate_hash()},
		)
		einv.reload()
		for row in einv.items:
			row.matched_item = item(self.company)
			row.match_status = "matched"
			if row.tax_rate in (20, 5.5):
				row.tax_account_head = tax_account(self.company, row.tax_rate)
		einv.save(ignore_permissions=True)
		return frappe.get_doc("Purchase Invoice", einv.convert_to_purchase_invoice())

	def test_vat_is_the_one_stated_on_the_invoice(self):
		pi = self.convert(lines=(("Service", 1, 100, 20),), stated_vat={20: 20.01})
		self.assertEqual(sum(tax.tax_amount for tax in pi.taxes), 20.01)
		self.assertEqual(pi.grand_total, 120.01)

	def test_each_rate_gets_its_stated_vat(self):
		pi = self.convert(lines=(("Service", 1, 100, 20), ("Livre", 2, 25, 5.5)))
		self.assertEqual(sorted(tax.tax_amount for tax in pi.taxes), [2.75, 20.0])
		self.assertEqual(pi.grand_total, 172.75)

	def test_a_rate_without_tax_account_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.convert(lines=(("Service", 1, 100, 20), ("Autre", 1, 100, 7)))

	def test_a_total_that_differs_from_the_invoice_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.convert(lines=(("Service", 1, 100, 20),), grand_total=130)
