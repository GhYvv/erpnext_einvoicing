# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

import datetime
import unittest

import frappe
from frappe.utils import get_datetime

from erpnext_einvoicing.providers.super_pdp_provider import SuperPdpProvider

PLATFORM = "SUPER PDP"


def utc(value):
	return get_datetime(value.replace("Z", "+00:00")).astimezone(datetime.UTC)


class FakePlatform(SuperPdpProvider):
	"""The platform answers a search with the given flows; downloads are skipped."""

	def __init__(self, flows, company="HCO"):
		super().__init__(frappe.get_doc("Approved Platforms", PLATFORM), frappe._dict(name=company))
		self.flows = flows
		self.searches = []

	def call_api(self, endpoint, method="GET", params=None, **kwargs):
		self.searches.append(params)
		return {"status_code": 200, "response": {"results": self.flows}}

	def _process_flow(self, flow_id, flow_data, sync_type):
		pass

	def _save_flow_doc(self, *args, **kwargs):
		pass


def flow(flow_id, updated_at):
	return {"flowId": flow_id, "updatedAt": updated_at}


class TestSyncCursor(unittest.TestCase):
	def setUp(self):
		frappe.db.delete("eInvoicing Sync Log", {"approved_platform": PLATFORM})
		self.time_zone = frappe.db.get_single_value("System Settings", "time_zone")
		frappe.db.set_single_value("System Settings", "time_zone", "Europe/Paris")
		frappe.clear_cache()

	def tearDown(self):
		frappe.db.set_single_value("System Settings", "time_zone", self.time_zone)
		frappe.clear_cache()
		frappe.db.rollback()

	def next_updated_after(self, company="HCO"):
		platform = FakePlatform([], company)
		platform.sync_flows("Purchase Invoice")
		return platform.searches[-1]["where"]["updatedAfter"]

	def test_cursor_is_never_ahead_of_utc(self):
		"""The cursor is sent with a `Z`: it must be UTC, not the system's local time."""
		now = datetime.datetime.now(datetime.UTC)
		FakePlatform(
			[flow("f1", (now - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))]
		).sync_flows("Purchase Invoice")
		self.assertLessEqual(utc(self.next_updated_after()), now)

	def test_cursor_follows_the_platform_clock(self):
		"""Flows beyond the search limit, or updated during the sync, must be read next time."""
		FakePlatform(
			[flow("f1", "2026-09-26T08:00:00.000Z"), flow("f2", "2026-09-26T08:05:00.000Z")]
		).sync_flows("Purchase Invoice")
		self.assertEqual(self.next_updated_after(), "2026-09-26T08:05:00.000Z")
