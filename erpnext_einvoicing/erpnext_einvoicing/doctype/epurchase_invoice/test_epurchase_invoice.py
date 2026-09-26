# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

import unittest

import frappe

from erpnext_einvoicing.tests.utils import ensure_test_company, make_epurchase_invoice


class TestEPurchaseInvoiceConversion(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = ensure_test_company()

	def convert(self, einv):
		return frappe.get_doc("Purchase Invoice", einv.convert_to_purchase_invoice())

	def test_invoice_is_converted_to_a_positive_purchase_invoice(self):
		pi = self.convert(make_epurchase_invoice(self.company))
		self.assertEqual(pi.is_return, 0)
		self.assertEqual(pi.grand_total, 120)

	def test_credit_note_is_converted_to_a_return(self):
		"""A received credit note (type 381) must become a debit note, not an invoice."""
		pi = self.convert(make_epurchase_invoice(self.company, is_credit_note=1))
		self.assertEqual(pi.is_return, 1)
		self.assertEqual(pi.items[0].qty, -2)
		self.assertEqual(pi.grand_total, -120)

	def test_credit_note_is_returned_against_the_converted_invoice(self):
		invoice = make_epurchase_invoice(self.company)
		invoice_pi = self.convert(invoice)
		invoice_pi.submit()
		credit = make_epurchase_invoice(self.company, is_credit_note=1, referenced=invoice.name)
		pi = self.convert(credit)
		self.assertEqual(pi.is_return, 1)
		self.assertEqual(pi.return_against, invoice_pi.name)
