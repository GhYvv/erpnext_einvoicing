# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

import datetime
from abc import ABC, abstractmethod
from datetime import timedelta
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe.utils import add_to_date, get_datetime, get_system_timezone, now_datetime

LIFECYCLE_STATUS_MAP = {
	"204": "Acknowledged",
	"205": "Approved",
	"207": "Disputed",
	"208": "Suspended",
	"210": "Rejected",
	"211": "PaymentTransmitted",
}

LIFECYCLE_STATUS_CODE_MAP = {
	"204": "45",
	"205": "1",
	"207": "46",
	"208": "39",
	"210": "45",
	"211": "47",
}

FLOW_TYPE_MAP = {
	"Purchase Invoice": "SupplierInvoice",
	"Sales Invoice": "CustomerInvoice",
}


def _parse_utc(value):
	"""An ISO 8601 timestamp from the platform, as an aware UTC datetime."""
	parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=datetime.UTC)
	return parsed.astimezone(datetime.UTC)


def _utc_to_system(value):
	"""Platform timestamp -> naive datetime in the system time zone, as Frappe stores it."""
	return _parse_utc(value).astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def _system_to_utc(value):
	"""Naive datetime in the system time zone -> aware UTC datetime."""
	return value.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(datetime.UTC)


class BaseProvider(ABC):
	"""
	Abstract base class for all e-invoicing platform providers.
	Credentials are stored per Company (einvoicing_* custom fields).
	Protocol/URL configuration is stored in Approved Platforms.
	"""

	def __init__(self, platform, company_doc):
		self.platform = platform
		self.company_doc = company_doc

	### Abstract methods

	@abstractmethod
	def get_access_token(self):
		"""Obtain a new access token and persist it. Returns the token string."""
		pass

	@abstractmethod
	def refresh_token(self):
		"""Refresh the current access token. Returns the new token string."""
		pass

	@abstractmethod
	def delete_access_token(self):
		"""Delete the stored access token. Returns True on success."""
		pass

	@abstractmethod
	def check_health(self):
		"""
		Check connectivity with the platform.
		Returns {"status": "ok"|"warning"|"error", "message": str}
		"""
		pass

	### Token helpers

	def get_base_url(self):
		url = (
			self.platform.prod_api_url
			if self.company_doc.einvoicing_live_mode
			else self.platform.test_api_url
		)
		if not url:
			frappe.throw(
				frappe._("API URL not configured on platform '{0}'.").format(self.platform.name),
				title=frappe._("Missing Configuration"),
			)
		return url if url.endswith("/") else url + "/"

	def is_token_expired(self):
		token = (
			self.company_doc.get_password("einvoicing_access_token")
			if self.company_doc.einvoicing_access_token
			else None
		)
		if not token:
			return True
		if not self.company_doc.einvoicing_token_expires_at:
			return True
		expires_at = get_datetime(self.company_doc.einvoicing_token_expires_at)
		now = get_datetime(now_datetime())
		return now >= (expires_at - timedelta(seconds=60))

	def save_token(self, token, expires_in=None):
		self.company_doc.db_set("einvoicing_access_token", token)
		if expires_in:
			expires_at = add_to_date(now_datetime(), seconds=int(expires_in))
			self.company_doc.db_set("einvoicing_token_expires_at", expires_at.strftime("%Y-%m-%d %H:%M:%S"))
		self.company_doc.einvoicing_access_token = token

	### XP Z12-013 common — extractable into BaseAfnorProvider if a third PA is added

	def _get_extra_headers(self):
		"""PA-specific additional headers. Override to add e.g. an API key header."""
		return {}

	def call_api(self, resource, method, params=None, extra_headers=None):
		if self.is_token_expired():
			self.get_access_token()

		base_url = self.get_base_url()
		url = f"{base_url}{resource}"

		headers = {
			"Authorization": f"Bearer {self.company_doc.get_password('einvoicing_access_token')}",
			"Content-Type": "application/json",
			**self._get_extra_headers(),
		}
		if extra_headers:
			headers.update(extra_headers)

		try:
			response = requests.request(
				method=method.upper(),
				url=url,
				headers=headers,
				json=params if method.upper() in ("POST", "PUT", "PATCH") else None,
				params=params if method.upper() == "GET" else None,
				timeout=30,
			)
		except requests.exceptions.ConnectionError:
			return {"status_code": 0, "response": "Connection error"}
		except requests.exceptions.Timeout:
			return {"status_code": 0, "response": "Timeout"}

		content_type = response.headers.get("Content-Type", "")
		if (
			"application/pdf" in content_type
			or "application/octet-stream" in content_type
			or response.content[:4] == b"%PDF"
		):
			return {"status_code": response.status_code, "response": response.content}

		try:
			return {"status_code": response.status_code, "response": response.json()}
		except Exception:
			return {"status_code": response.status_code, "response": response.text}

	def _call_api_multipart(self, resource, files_payload):
		if self.is_token_expired():
			self.get_access_token()

		base_url = self.get_base_url()
		url = f"{base_url}{resource}"

		headers = {
			"Authorization": f"Bearer {self.company_doc.get_password('einvoicing_access_token')}",
			**self._get_extra_headers(),
		}
		try:
			response = requests.post(url, headers=headers, files=files_payload, timeout=30)
		except requests.exceptions.ConnectionError:
			return {"status_code": 0, "response": "Connection error"}
		except requests.exceptions.Timeout:
			return {"status_code": 0, "response": "Timeout"}

		try:
			return {"status_code": response.status_code, "response": response.json()}
		except Exception:
			return {"status_code": response.status_code, "response": response.text}

	def check_pending_flows(self, sync_type, company=None):
		payload = self._build_search_payload(sync_type, limit=1)
		result = self.call_api("flows/search", "POST", params=payload)
		if result["status_code"] not in (200, 202):
			return {"has_pending": False, "total": 0, "error": f"HTTP {result['status_code']}"}
		total = result["response"].get("total", 0)
		return {"has_pending": total > 0, "total": total}

	def sync_flows(self, sync_type, company=None):
		payload = self._build_search_payload(sync_type, limit=1000)
		result = self.call_api("flows/search", "POST", params=payload)
		if result["status_code"] not in (200, 202):
			return {
				"status": "error",
				"message": frappe._("Failed to reach platform (HTTP {0}).").format(result["status_code"]),
			}
		flows_raw = result["response"].get("results", [])
		if not flows_raw:
			return {
				"status": "ok",
				"message": frappe._("No flows to synchronize."),
				"total": 0,
				"synced": 0,
				"skipped": 0,
				"errors": 0,
			}
		total = len(flows_raw)
		flows = sorted(flows_raw, key=lambda f: f.get("updatedAt", ""))

		existing_flow_ids = set(
			frappe.db.get_all(
				"eInvoicing Flow",
				filters={"approved_platform": self.platform.name},
				pluck="flow_id",
			)
		)

		synced = skipped = errors = 0
		# The cursor follows the platform's clock: the latest `updatedAt` seen.
		# Flows beyond the search limit, or updated during this sync, are
		# then read next time; flows seen twice are skipped by flow_id.
		updated = [f["updatedAt"] for f in flows if f.get("updatedAt")]
		sync_date = _utc_to_system(max(updated, key=_parse_utc)) if updated else now_datetime()

		for flow in flows:
			flow_id = flow.get("flowId")
			if not flow_id:
				errors += 1
				continue
			if flow_id in existing_flow_ids:
				skipped += 1
				continue
			try:
				self._process_flow(flow_id, flow, sync_type)
				synced += 1
			except Exception:
				frappe.log_error(frappe.get_traceback(), f"eInvoicing sync error — flowId {flow_id}")
				errors += 1

		status = "ok" if errors == 0 else ("partial" if synced > 0 else "error")
		self._save_sync_log(sync_type, sync_date, status, total, synced, skipped, errors)

		return {
			"status": status,
			"message": frappe._("{0} synced, {1} skipped, {2} errors out of {3} flows.").format(
				synced, skipped, errors, total
			),
			"total": total,
			"synced": synced,
			"skipped": skipped,
			"errors": errors,
		}

	def _process_flow(self, flow_id, flow_data, sync_type):
		result = self.call_api(
			f"flows/{flow_id}",
			"GET",
			params={"docType": "Converted"},
			extra_headers={"Accept": "application/octet-stream"},
		)
		if result["status_code"] not in (200, 202):
			frappe.throw(
				frappe._("Failed to download flow {0} (HTTP {1}).").format(flow_id, result["status_code"])
			)
		pdf_content = result["response"]
		if not isinstance(pdf_content, bytes):
			frappe.throw(frappe._("Expected binary PDF content for flow {0}.").format(flow_id))

		from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import create_e_purchase_invoice

		einvoice = create_e_purchase_invoice(pdf_content, flow_data)
		self._save_flow_doc(flow_id, flow_data, sync_type, "ePurchase Invoice", einvoice.name)

	def _save_flow_doc(self, flow_id, flow_data, sync_type, document_type, document_name):
		doc = frappe.new_doc("eInvoicing Flow")
		doc.flow_id = flow_id
		doc.approved_platform = self.platform.name
		doc.flow_type = sync_type
		doc.flow_direction = flow_data.get("flowDirection", "")
		doc.ack_status = flow_data.get("acknowledgement", {}).get("status", "")
		doc.tracking_id = flow_data.get("trackingId", "")
		doc.document_type = document_type or ""
		doc.document_name = document_name or ""
		submitted_at = flow_data.get("submittedAt")
		updated_at = flow_data.get("updatedAt")
		if submitted_at:
			doc.submitted_at = get_datetime(submitted_at).replace(tzinfo=None)
		if updated_at:
			doc.updated_at = get_datetime(updated_at).replace(tzinfo=None)
		doc.insert(ignore_permissions=True)

	def _save_sync_log(self, sync_type, sync_date, status, total, synced, skipped, errors):
		doc = frappe.new_doc("eInvoicing Sync Log")
		doc.sync_type = sync_type
		doc.approved_platform = self.platform.name
		doc.company = self.company_doc.name
		doc.last_sync_date = sync_date
		doc.last_sync_status = status
		doc.flows_total = total
		doc.flows_synced = synced
		doc.flows_skipped = skipped
		doc.flows_error = errors
		doc.insert(ignore_permissions=True)

	def _build_search_payload(self, sync_type, limit=100):
		# One cursor per company: the platform credentials, hence the flows, are
		# per company. Logs written before the company was recorded are the
		# fallback, so that an upgrade does not read everything again.
		last_sync_date = None
		for company in (self.company_doc.name, ("is", "not set")):
			last_sync_date = frappe.db.get_value(
				"eInvoicing Sync Log",
				filters={
					"sync_type": sync_type,
					"approved_platform": self.platform.name,
					"company": company,
					"last_sync_status": "ok",
				},
				fieldname="last_sync_date",
				order_by="last_sync_date desc",
			)
			if last_sync_date:
				break
		updated_after = (
			_system_to_utc(get_datetime(last_sync_date)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
			if last_sync_date
			else "1970-01-01T00:00:00.000Z"
		)
		return {
			"where": {
				"updatedAfter": updated_after,
				"flowType": [FLOW_TYPE_MAP.get(sync_type, "SupplierInvoice")],
				"flowDirection": ["In"],
			},
			"limit": limit,
		}

	### Lifecycle (AFNOR XP Z12-013)

	def _send_cdar(self, xml_bytes, filename, flow_info):
		from io import BytesIO

		files_payload = {
			"file": (filename, BytesIO(xml_bytes), "application/xml"),
			"flowInfo": (None, frappe.as_json(flow_info), "text/plain"),
		}
		result = self._call_api_multipart("flows", files_payload)
		if result["status_code"] not in (200, 202):
			frappe.throw(
				frappe._("Failed to send CDAR (HTTP {0}): {1}").format(
					result["status_code"], result.get("response", "")
				),
				title=frappe._("Lifecycle Send Error"),
			)
		return result

	def send_lifecycle(self, status_code, doc, refusal_reasons=None):
		from pyfrctc import generate_cdar

		if status_code not in LIFECYCLE_STATUS_MAP:
			frappe.throw(
				frappe._("Unknown lifecycle status code: {0}").format(status_code),
				title=frappe._("Lifecycle Error"),
			)
		cdar_dict = self._build_cdar_data_dict(status_code, doc, refusal_reasons)
		try:
			xml_bytes = generate_cdar(cdar_dict)
		except Exception as e:
			frappe.throw(
				frappe._("Failed to generate CDAR XML: {0}").format(str(e)),
				title=frappe._("CDAR Generation Error"),
			)
		filename = f"Lifecycle-{status_code}-{doc.invoice_number}.xml"[:255]
		flow_info = self._build_lifecycle_flow_info(filename, doc)
		result = self._send_cdar(xml_bytes, filename, flow_info)
		cdar_flow_id = ""
		if isinstance(result.get("response"), dict):
			cdar_flow_id = result["response"].get("flowId", "")
		if getattr(doc, "name", None):
			self._insert_lifecycle_log(doc.name, status_code, cdar_flow_id)
		return result

	def _build_cdar_data_dict(self, status_code, doc, refusal_reasons=None):
		now = datetime.datetime.now()
		buyer_siret = (doc.buyer_siret or "").replace(" ", "")
		supplier_siret = (doc.supplier_siret or doc.supplier_siren or "").replace(" ", "")
		supplier_siren = supplier_siret[:9]

		if not buyer_siret:
			frappe.throw(
				frappe._("Cannot send lifecycle: no buyer SIRET on {0}.").format(doc.name),
				title=frappe._("Lifecycle Error"),
			)
		if not supplier_siret:
			frappe.throw(
				frappe._("Cannot send lifecycle: no supplier SIRET or SIREN on {0}.").format(doc.name),
				title=frappe._("Lifecycle Error"),
			)

		company_name = frappe.db.get_value("Company", doc.company, "company_name") or doc.company or ""
		invoice_date_for_id = str(doc.invoice_date or "")[:10]
		cdar_id = (
			f"{doc.invoice_number}_380_{invoice_date_for_id}#{status_code}_{now.strftime('%Y%m%d%H%M%S')}"
		)

		invoice_date_dt = None
		if doc.invoice_date:
			if isinstance(doc.invoice_date, datetime.datetime):
				invoice_date_dt = doc.invoice_date
			elif isinstance(doc.invoice_date, datetime.date):
				invoice_date_dt = datetime.datetime.combine(doc.invoice_date, datetime.time.min)
			else:
				try:
					invoice_date_dt = datetime.datetime.strptime(str(doc.invoice_date), "%Y-%m-%d")
				except ValueError:
					pass

		cdar_dict = {
			"MDT-2": "REGULATED",  # Business process (REGULATED = regulated B2B)
			"MDT-3": "urn.cpro.gouv.fr:1p0:CDV:invoice",  # Guideline ID (CDV invoice profile)
			"MDT-4": cdar_id,  # Unique CDAR identifier
			"MDT-8": now,  # CDAR issue date/time
			"MDT-21": "WK",  # CDAR sender role (WK = platform)
			"MDT-38": {"0002": buyer_siret},  # Buyer SIRET (invoice recipient)
			"MDT-39": company_name,  # Buyer name
			"MDT-40": "BY",  # Buyer role (BY = buyer)
			"MDT-57": {"0002": supplier_siren},  # Supplier SIREN (invoice issuer)
			"MDT-58": doc.supplier_name_raw or "",  # Supplier name
			"MDT-59": "SE",  # Supplier role (SE = seller)
			"MDT-73": doc.get("supplier_uriid")
			or supplier_siren,  # Supplier directory identifier (URIID or SIREN)
			"MDT-73-1": "0225",  # Directory identifier scheme (0225 = French SIREN/SIRET)
			"MDT-74": False,  # Multiple references indicator
			"MDT-77": 23,  # Acknowledgement document type (23 = acknowledgement)
			"MDT-78": now,  # Acknowledgement issue date/time
			"MDT-87": doc.invoice_number or "",  # Referenced invoice number
			"MDT-88": LIFECYCLE_STATUS_CODE_MAP.get(
				status_code, "45"
			),  # UN/CEFACT status code (1=accepted, 45=in process, 47=paid...)
			"MDT-91": "380",  # Referenced document type (380 = invoice, 381 = credit note)
			"MDT-100": invoice_date_dt.date()
			if invoice_date_dt
			else datetime.date.today(),  # Original invoice date
			"MDT-105": status_code,  # Lifecycle status code (204, 205, 210, 211...)
			"MDT-106": LIFECYCLE_STATUS_MAP[status_code],  # Lifecycle status label
			"MDT-129": {"0002": supplier_siren},  # Supplier legal identifier (SIRET)
		}
		if invoice_date_dt:
			cdar_dict["MDT-95"] = invoice_date_dt  # Invoice receipt date/time
		if refusal_reasons:
			cdar_dict["MDG-37"] = (
				refusal_reasons  # Refusal/dispute reasons (MDT-113 = code, MDT-126 = comment)
			)
		return cdar_dict

	def _build_lifecycle_flow_info(self, filename, doc):
		return {"flowSyntax": "CDAR", "name": filename}

	def _insert_lifecycle_log(self, einvoice_name, status_code, cdar_flow_id):
		try:
			log = frappe.new_doc("eInvoicing Lifecycle Log")
			log.parent = einvoice_name
			log.parenttype = "ePurchase Invoice"
			log.parentfield = "lifecycle_logs"
			log.status_code = status_code
			log.status_label = frappe._(LIFECYCLE_STATUS_MAP.get(status_code, status_code))
			log.cdar_flow_id = cdar_flow_id
			log.sent_at = frappe.utils.now_datetime()
			log.ack_status = "pending" if cdar_flow_id else "error"
			log.error_type = None if cdar_flow_id else "platform"
			log.ack_message = None if cdar_flow_id else frappe._("No flow ID returned by platform")
			log.insert(ignore_permissions=True)

			logs_ordered = frappe.get_all(
				"eInvoicing Lifecycle Log",
				filters={"parent": einvoice_name},
				fields=["name", "sent_at"],
				order_by="sent_at asc",
			)
			for i, l in enumerate(logs_ordered, start=1):
				frappe.db.set_value("eInvoicing Lifecycle Log", l["name"], "idx", i)

			frappe.db.commit()
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"eInvoicing lifecycle log insert — {einvoice_name}",
			)
