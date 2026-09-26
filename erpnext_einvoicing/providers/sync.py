# Copyright (c) 2026, Scopen and contributors
# For license information, please see license.txt

import frappe

### Helpers


def _get_provider(company=None):
	if not company:
		company = frappe.defaults.get_user_default("company")
	if not company:
		frappe.throw(frappe._("No company selected."))
	company_doc = frappe.get_doc("Company", company)
	if not company_doc.einvoicing_approved_platform:
		frappe.throw(frappe._("No Approved Platform configured for company '{0}'.").format(company))
	platform = frappe.get_doc("Approved Platforms", company_doc.einvoicing_approved_platform)
	if not platform.is_enabled:
		frappe.throw(frappe._("Platform '{0}' is disabled.").format(platform.name))

	if platform.provider_type == "Esalink":
		from erpnext_einvoicing.providers.esalink_provider import EsalinkProvider

		return EsalinkProvider(platform, company_doc)
	if platform.provider_type == "SuperPDP":
		from erpnext_einvoicing.providers.super_pdp_provider import SuperPdpProvider

		return SuperPdpProvider(platform, company_doc)

	frappe.throw(
		frappe._("Unsupported provider type: '{0}'.").format(platform.provider_type),
		title=frappe._("Configuration Error"),
	)


### Provider whitelisted methods


@frappe.whitelist()
def healthcheck(company=None):
	frappe.only_for("System Manager")
	return _get_provider(company).check_health()


@frappe.whitelist()
def recreate_access_token(company=None):
	frappe.only_for("System Manager")
	provider = _get_provider(company)
	try:
		provider.get_access_token()
		return {"status": "ok", "message": frappe._("Access token successfully generated.")}
	except frappe.ValidationError as e:
		return {"status": "error", "message": str(e)}


@frappe.whitelist()
def delete_access_token(company=None):
	frappe.only_for("System Manager")
	provider = _get_provider(company)
	try:
		provider.delete_access_token()
		return {"status": "ok", "message": frappe._("Access token deleted.")}
	except Exception as e:
		return {"status": "error", "message": str(e)}


@frappe.whitelist()
def check_pending_flows(sync_type="Purchase Invoice", company=None):
	frappe.only_for("System Manager")
	try:
		return _get_provider(company).check_pending_flows(sync_type)
	except Exception as e:
		return {"has_pending": False, "total": 0, "error": str(e)}


@frappe.whitelist()
def sync_flows(sync_type="Purchase Invoice", company=None):
	frappe.only_for("System Manager")
	return _get_provider(company).sync_flows(sync_type)


### Inbox whitelisted methods


@frappe.whitelist()
def get_buying_settings():
	buying_settings = frappe.get_single("Buying Settings")
	return {
		"po_required": buying_settings.po_required == "Yes",
		"pr_required": buying_settings.pr_required == "Yes",
	}


@frappe.whitelist()
def convert_ethirdparty_for_po(name):
	from erpnext_einvoicing.erpnext_einvoicing.utils.conversion import _create_supplier_from_ethirdparty

	doc = frappe.get_doc("ePurchase Invoice", name)
	if not doc.ethirdparty:
		return {"status": "error", "error": "No eThirdParty linked to this invoice"}
	try:
		supplier_name = _create_supplier_from_ethirdparty(doc.ethirdparty)
		doc.db_set("matched_supplier", supplier_name)
		doc.db_set("supplier_match_status", "matched")
		doc.reload()
		doc._update_conversion_status()
		doc.db_set("conversion_status", doc.conversion_status)
		frappe.db.commit()
		items = [
			{
				"item_code": item.matched_item,
				"qty": item.qty,
				"uom": item.uom,
				"rate": item.unit_price,
			}
			for item in doc.items
			if item.match_status == "matched" and item.matched_item
		]
		return {"status": "ok", "supplier": supplier_name, "items": items}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "convert_ethirdparty_for_po")
		return {"status": "error", "error": frappe.get_last_doc("Error Log").error}


@frappe.whitelist()
def link_purchase_order(name, purchase_order):
	from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import _auto_match_items

	doc = frappe.get_doc("ePurchase Invoice", name)
	doc.purchase_order = purchase_order

	_auto_match_items(doc)
	doc.reload()
	doc.purchase_order = purchase_order

	for item in doc.items:
		if item.match_status != "matched" or not item.matched_item:
			continue
		po_line = frappe.db.get_value(
			"Purchase Order Item",
			{"parent": purchase_order, "item_code": item.matched_item},
			["name", "qty"],
			as_dict=True,
		)
		if not po_line:
			continue
		billed_qty = frappe.db.sql(
			"""
            SELECT COALESCE(SUM(qty), 0)
            FROM `tabPurchase Invoice Item`
            WHERE po_detail = %s
              AND docstatus = 1
			""",
			po_line.name,
		)[0][0]
		remaining = po_line.qty - billed_qty
		item.purchase_order = purchase_order
		item.po_detail = po_line.name
		item.po_match_status = "matched" if remaining == item.qty else "partial"

	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def unlink_purchase_order(name):
	doc = frappe.get_doc("ePurchase Invoice", name)
	doc.purchase_order = None
	for item in doc.items:
		item.purchase_order = None
		item.po_detail = None
		item.po_match_status = None
	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def link_purchase_receipt(name, purchase_receipt):
	from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import _auto_match_items

	doc = frappe.get_doc("ePurchase Invoice", name)

	_auto_match_items(doc)
	doc.reload()
	doc.purchase_receipt = purchase_receipt

	for item in doc.items:
		if item.match_status != "matched" or not item.matched_item:
			continue
		pr_line = frappe.db.get_value(
			"Purchase Receipt Item",
			{"parent": purchase_receipt, "item_code": item.matched_item},
			["name", "qty"],
			as_dict=True,
		)
		if not pr_line:
			continue
		billed_qty = frappe.db.sql(
			"""
            SELECT COALESCE(SUM(qty), 0)
            FROM `tabPurchase Invoice Item`
            WHERE pr_detail = %s
              AND docstatus = 1
			""",
			pr_line.name,
		)[0][0]
		remaining = pr_line.qty - billed_qty
		item.purchase_receipt = purchase_receipt
		item.pr_detail = pr_line.name
		item.pr_match_status = "matched" if remaining == item.qty else "partial"

	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def unlink_purchase_receipt(name):
	doc = frappe.get_doc("ePurchase Invoice", name)
	doc.purchase_receipt = None
	for item in doc.items:
		item.purchase_receipt = None
		item.pr_detail = None
		item.pr_match_status = None
	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def get_einvoicing_inbox(date_from=None, date_to=None, company=None):
	filters = {"conversion_status": ["in", ["pending", "ready", "converted", "refused"]]}
	if company:
		filters["company"] = company
	if date_from and date_to:
		filters["invoice_date"] = ["between", [date_from, date_to]]
	elif date_from:
		filters["invoice_date"] = [">=", date_from]
	elif date_to:
		filters["invoice_date"] = ["<=", date_to]
	invoices = frappe.get_all(
		"ePurchase Invoice",
		filters=filters,
		fields=[
			"name",
			"conversion_status",
			"purchase_invoice",
			"invoice_number",
			"invoice_date",
			"currency",
			"total_ht",
			"total_vat",
			"total_ttc",
			"supplier_name_raw",
			"supplier_siret",
			"supplier_siren",
			"supplier_vat",
			"supplier_match_status",
			"sirene_status",
			"matched_supplier",
			"ethirdparty",
			"purchase_order",
			"purchase_receipt",
			"approved_platform",
			"purchase_invoice",
			"company",
			"buyer_siret",
			"is_credit_note",
			"referenced_invoice_number",
			"referenced_epurchase_invoice",
		],
		order_by="creation desc",
	)

	for inv in invoices:
		inv["items"] = frappe.get_all(
			"ePurchase Invoice Item",
			filters={"parent": inv["name"]},
			fields=[
				"idx",
				"item_description_raw",
				"item_ref_raw",
				"qty",
				"uom",
				"unit_price",
				"amount",
				"tax_rate",
				"tax_account_head",
				"match_status",
				"matched_item",
				"purchase_order",
				"po_detail",
				"po_match_status",
				"purchase_receipt",
				"pr_detail",
				"pr_match_status",
			],
			order_by="idx asc",
		)

		attachment = frappe.db.get_value(
			"File",
			{
				"attached_to_doctype": "ePurchase Invoice",
				"attached_to_name": inv["name"],
			},
			"file_url",
		)
		inv["pdf_url"] = attachment or None

		last_log_list = frappe.get_all(
			"eInvoicing Lifecycle Log",
			filters={"parent": inv["name"]},
			fields=["name", "status_code", "status_label", "ack_status", "ack_message", "error_type"],
			order_by="sent_at desc",
			limit=1,
		)
		inv["last_lifecycle_log"] = last_log_list[0] if last_log_list else None

		company = inv.get("company")
		for item in inv["items"]:
			if item.get("tax_account_head"):
				item["tax_account_name"] = frappe.db.get_value(
					"Account", item["tax_account_head"], "account_name"
				)
			elif item.get("tax_rate") and company:
				item["tax_account_name"] = frappe.db.get_value(
					"Account",
					{
						"company": company,
						"account_type": "Tax",
						"tax_rate": item["tax_rate"],
						"root_type": "Asset",
					},
					"account_name",
				)
			else:
				item["tax_account_name"] = None

		if inv.get("ethirdparty"):
			inv["ethirdparty_doc"] = frappe.db.get_value(
				"eThirdParty",
				inv["ethirdparty"],
				[
					"name",
					"status",
					"party_name",
					"siret",
					"zip",
					"city",
					"country_code",
				],
				as_dict=True,
			)

		if inv.get("is_credit_note") and inv.get("referenced_epurchase_invoice"):
			inv["ref_status"] = frappe.db.get_value(
				"ePurchase Invoice",
				inv["referenced_epurchase_invoice"],
				"conversion_status",
			)
		else:
			inv["ref_status"] = None

	return invoices


@frappe.whitelist()
def match_supplier(name, matched_supplier, apply_to_all=0):
	apply_to_all = frappe.utils.cint(apply_to_all)

	### Vérification cohérence SIRET
	siret = frappe.db.get_value("ePurchase Invoice", name, "supplier_siret")
	if siret:
		existing_siret = frappe.db.get_value("Supplier", matched_supplier, "siret")
		existing_siren = frappe.db.get_value("Supplier", matched_supplier, "siren")
		existing_tax_id = frappe.db.get_value("Supplier", matched_supplier, "tax_id")

		supplier_identifiers = {v for v in [existing_siret, existing_siren, existing_tax_id] if v}

		if supplier_identifiers:
			siret_match = any(
				siret.startswith(id) or id.startswith(siret) or siret == id for id in supplier_identifiers
			)
			if not siret_match:
				return {
					"status": "warning",
					"message": frappe._(
						"Supplier '{0}' identifiers ({1}) don't match invoice SIRET {2}."
					).format(
						matched_supplier,
						", ".join(supplier_identifiers),
						siret,
					),
				}

	frappe.db.set_value(
		"ePurchase Invoice",
		name,
		{
			"matched_supplier": matched_supplier,
			"supplier_match_status": "matched",
		},
	)

	from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import (
		_auto_match_items,
		_auto_match_po,
		_auto_match_pr,
	)

	doc = frappe.get_doc("ePurchase Invoice", name)
	_auto_match_items(doc)
	_auto_match_po(doc)
	_auto_match_pr(doc)
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)

	if apply_to_all and siret:
		others = frappe.get_all(
			"ePurchase Invoice",
			filters={
				"supplier_siret": siret,
				"name": ["!=", name],
				"supplier_match_status": "unmatched",
				"conversion_status": ["!=", "converted"],
			},
			pluck="name",
		)
		for other in others:
			frappe.db.set_value(
				"ePurchase Invoice",
				other,
				{
					"matched_supplier": matched_supplier,
					"supplier_match_status": "matched",
				},
			)
			inv = frappe.get_doc("ePurchase Invoice", other)
			inv._update_conversion_status()
			inv.db_set("conversion_status", inv.conversion_status)

	frappe.db.commit()

	doc.reload()
	matched_count = sum(1 for item in doc.items if item.match_status == "matched")
	return {
		"status": "ok",
		"supplier": matched_supplier,
		"matched_items": matched_count,
		"total_items": len(doc.items),
		"conversion_status": doc.conversion_status,
	}


@frappe.whitelist()
def count_similar_unmatched(name, match_type):
	doc = frappe.get_doc("ePurchase Invoice", name)

	if match_type == "supplier":
		if not doc.supplier_siret:
			return {"count": 0}
		count = frappe.db.count(
			"ePurchase Invoice",
			{
				"supplier_siret": doc.supplier_siret,
				"name": ["!=", name],
				"supplier_match_status": "unmatched",
				"conversion_status": ["!=", "converted"],
			},
		)
		return {"count": count}

	if match_type == "item":
		item_idx = frappe.form_dict.get("item_idx")
		if not item_idx:
			return {"count": 0}
		doc = frappe.get_doc("ePurchase Invoice", name)
		ref_raw = next((i.item_ref_raw for i in doc.items if i.idx == int(item_idx)), None)
		if not ref_raw:
			return {"count": 0}
		count = frappe.db.count(
			"ePurchase Invoice Item",
			{
				"item_ref_raw": ref_raw,
				"match_status": "unmatched",
				"parent": ["!=", name],
			},
		)
		return {"count": count}

	if match_type == "supplier_matched":
		siret = frappe.db.get_value("ePurchase Invoice", name, "supplier_siret")
		if not siret:
			return {"count": 0}
		count = frappe.db.count(
			"ePurchase Invoice",
			{
				"supplier_siret": siret,
				"name": ["!=", name],
				"supplier_match_status": "matched",
				"conversion_status": ["!=", "converted"],
			},
		)
		return {"count": count}

	if match_type == "item_matched":
		item_idx = frappe.form_dict.get("item_idx")
		if not item_idx:
			return {"count": 0}
		doc = frappe.get_doc("ePurchase Invoice", name)
		ref_raw = next((i.item_ref_raw for i in doc.items if i.idx == int(item_idx)), None)
		if not ref_raw:
			return {"count": 0}
		count = frappe.db.count(
			"ePurchase Invoice Item",
			{
				"item_ref_raw": ref_raw,
				"match_status": "matched",
				"parent": ["!=", name],
			},
		)
		return {"count": count}

	return {"count": 0}


@frappe.whitelist()
def rematch_supplier(name):
	doc = frappe.get_doc("ePurchase Invoice", name)
	siret = (doc.supplier_siret or "").replace(" ", "")
	name_raw = doc.supplier_name_raw or ""

	if siret or doc.supplier_siren:
		from erpnext_einvoicing.erpnext_einvoicing.utils.identifiers import find_supplier

		supplier = find_supplier(siret, doc.supplier_siren)
		if supplier:
			doc.db_set("matched_supplier", supplier)
			doc.db_set("supplier_match_status", "matched")
			frappe.db.commit()
			return {"status": "ok", "supplier": supplier}

	if name_raw:
		supplier = frappe.db.get_value(
			"Supplier",
			{"supplier_name": ["like", f"%{name_raw}%"]},
			"name",
		)
		if supplier:
			doc.db_set("matched_supplier", supplier)
			doc.db_set("supplier_match_status", "matched")
			frappe.db.commit()
			return {"status": "ok", "supplier": supplier}

	return {"status": "not_found"}


def _rematch_all_pending():
	invoices = frappe.get_all(
		"ePurchase Invoice",
		filters={"conversion_status": ["in", ["pending"]]},
		pluck="name",
	)
	for name in invoices:
		doc = frappe.get_doc("ePurchase Invoice", name)
		if doc.supplier_match_status == "unmatched":
			from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import _auto_match_supplier

			_auto_match_supplier(
				doc,
				{
					"supplier_siret": doc.supplier_siret,
					"supplier_name_raw": doc.supplier_name_raw,
				},
			)
		from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import _auto_match_items

		_auto_match_items(doc)


@frappe.whitelist()
def rematch_all():
	from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import (
		_auto_match_items,
		_auto_match_po,
		_auto_match_pr,
		_auto_match_supplier,
	)

	invoices = frappe.get_all(
		"ePurchase Invoice",
		filters={"conversion_status": ["in", ["pending", "ready"]]},
		pluck="name",
	)

	for name in invoices:
		doc = frappe.get_doc("ePurchase Invoice", name)
		data = {
			"supplier_siret": doc.supplier_siret,
			"supplier_name_raw": doc.supplier_name_raw,
		}
		_auto_match_supplier(doc, data)
		doc.reload()
		_auto_match_items(doc)
		doc.reload()
		_auto_match_po(doc)
		doc.reload()
		_auto_match_pr(doc)
		doc.reload()
		doc._update_conversion_status()
		doc.db_set("conversion_status", doc.conversion_status)

	frappe.db.commit()
	return {
		"status": "ok",
		"message": frappe._("{0} invoice(s) processed.").format(len(invoices)),
	}


@frappe.whitelist()
def unlink_matched_supplier(name, apply_to_all=0):
	apply_to_all = frappe.utils.cint(apply_to_all)
	doc = frappe.get_doc("ePurchase Invoice", name)
	siret = doc.supplier_siret
	ethirdparty_name = doc.ethirdparty

	frappe.db.set_value(
		"ePurchase Invoice",
		name,
		{
			"matched_supplier": None,
			"supplier_match_status": "unmatched",
			"ethirdparty": None,
			"sirene_status": None,
			"conversion_status": "pending",
		},
	)

	doc.reload()
	for item in doc.items:
		item.matched_item = None
		item.match_status = "unmatched"
	doc.save(ignore_permissions=True)

	if apply_to_all and siret:
		others = frappe.get_all(
			"ePurchase Invoice",
			filters={
				"supplier_siret": siret,
				"name": ["!=", name],
				"conversion_status": ["!=", "converted"],
			},
			pluck="name",
		)
		for other_name in others:
			frappe.db.set_value(
				"ePurchase Invoice",
				other_name,
				{
					"matched_supplier": None,
					"supplier_match_status": "unmatched",
					"ethirdparty": None,
					"sirene_status": None,
					"conversion_status": "pending",
				},
			)
			other_doc = frappe.get_doc("ePurchase Invoice", other_name)
			for item in other_doc.items:
				item.matched_item = None
				item.match_status = "unmatched"
			other_doc.save(ignore_permissions=True)

	if ethirdparty_name:
		others = frappe.db.count("ePurchase Invoice", {"ethirdparty": ethirdparty_name})
		if not others:
			frappe.delete_doc("eThirdParty", ethirdparty_name, ignore_permissions=True)

	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def enrich_from_siret(name):
	import requests as req

	doc = frappe.get_doc("ePurchase Invoice", name)

	siret = (doc.supplier_siret or doc.supplier_siren or "").replace(" ", "")
	if not siret:
		return {"status": "error", "error": frappe._("No SIRET on this invoice.")}

	ethirdparty_name = doc.ethirdparty or frappe.db.get_value("eThirdParty", {"siret": siret}, "name")

	if ethirdparty_name:
		ethirdparty = frappe.get_doc("eThirdParty", ethirdparty_name)
	else:
		ethirdparty = frappe.new_doc("eThirdParty")
		ethirdparty.party_type = "Supplier"
		ethirdparty.siret = doc.supplier_siret or ""
		ethirdparty.siren = doc.supplier_siren or ""
		ethirdparty.vat_number = doc.supplier_vat or ""
		ethirdparty.party_name = doc.supplier_name_raw or ""
		ethirdparty.status = "pending"
		ethirdparty.flags.ignore_mandatory = True

	try:
		response = req.get(
			"https://recherche-entreprises.api.gouv.fr/search",
			params={"q": siret, "per_page": 1},
			timeout=10,
		)
		response.raise_for_status()
		data = response.json()
	except Exception as e:
		return {"status": "error", "error": frappe._("SIRENE API error: {0}").format(str(e))}

	results = data.get("results", [])
	if not results:
		frappe.db.set_value("ePurchase Invoice", name, "sirene_status", "not_found")
		frappe.db.commit()
		return {
			"status": "not_found",
			"data": {
				"siret": siret,
				"party_name": doc.supplier_name_raw or "",
				"vat_number": doc.supplier_vat or "",
			},
		}

	is_new = ethirdparty.is_new()
	company = results[0]
	siege = company.get("siege", {})

	ethirdparty.party_name = company.get("nom_complet") or ethirdparty.party_name
	ethirdparty.address_line1 = siege.get("adresse", "")
	ethirdparty.zip = siege.get("code_postal", "")
	ethirdparty.city = siege.get("libelle_commune", "")
	ethirdparty.country_code = "FR"
	ethirdparty.sirene_raw = frappe.as_json(company)

	if "erpnext_france" in frappe.get_installed_apps():
		siren = company.get("siren", siret[:9])
		code_naf = siege.get("activite_principale", "") or company.get("activite_principale", "")
		legal_form = company.get("nature_juridique", "")
		for fieldname, value in (
			("siret", siret),
			("siren", siren),
			("code_naf", code_naf),
			("legal_form", legal_form),
		):
			if value:
				ethirdparty.set(fieldname, value)
	else:
		siren = code_naf = legal_form = ""

	### Dry-run validation

	test_supplier = frappe.new_doc("Supplier")
	test_supplier.supplier_name = ethirdparty.party_name
	test_supplier.supplier_group = frappe.db.get_single_value(
		"Buying Settings", "supplier_group"
	) or frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
	test_supplier.tax_id = siret

	def _is_custom_field_active(field):
		if not field.is_custom_field:
			return True
		if field.fieldtype == "Link" and field.options:
			return frappe.db.table_exists("tab" + field.options)
		return True

	missing_fields = [
		field.label
		for field in frappe.get_meta("Supplier").fields
		if field.reqd
		and not test_supplier.get(field.fieldname)
		and not field.fieldname.startswith("custom_")
		and _is_custom_field_active(field)
	]

	try:
		test_supplier.run_method("validate")
	except Exception as e:
		missing_fields.append(str(e))

	mandatory_custom_fieldnames = frappe.get_all(
		"Custom Field",
		filters={"dt": "Supplier", "reqd": 1},
		pluck="fieldname",
	)
	custom_data = {f: ethirdparty.get(f) for f in mandatory_custom_fieldnames}

	extra_data = (
		{"siren": siren, "code_naf": code_naf, "legal_form": legal_form}
		if siren or code_naf or legal_form
		else {}
	)

	if missing_fields:
		ethirdparty.status = "warning"

	if is_new:
		ethirdparty.insert(ignore_permissions=True)
		doc.db_set("ethirdparty", ethirdparty.name)
	else:
		ethirdparty.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "warning" if missing_fields else "ok",
		"data": {
			"siret": siret,
			"party_name": ethirdparty.party_name,
			"address_line1": ethirdparty.address_line1,
			"zip": ethirdparty.zip,
			"city": ethirdparty.city,
			"country_code": ethirdparty.country_code,
			**extra_data,
			**custom_data,
		},
		"missing_fields": missing_fields,
	}


@frappe.whitelist()
def get_mandatory_supplier_custom_fields():
	return frappe.get_all(
		"Custom Field",
		filters={"dt": "Supplier", "reqd": 1},
		fields=["fieldname", "label", "fieldtype", "options"],
	)


@frappe.whitelist()
def save_ethirdparty(invoice_name, data, supplier_group, apply_to_all=0):
	apply_to_all = frappe.utils.cint(apply_to_all)
	if isinstance(data, str):
		import json

		data = json.loads(data)

	doc = frappe.get_doc("ePurchase Invoice", invoice_name)
	siret = data.get("siret", "")

	ethirdparty_name = doc.ethirdparty or frappe.db.get_value("eThirdParty", {"siret": siret}, "name")

	if ethirdparty_name:
		ethirdparty = frappe.get_doc("eThirdParty", ethirdparty_name)
	else:
		ethirdparty = frappe.new_doc("eThirdParty")
		ethirdparty.party_type = "Supplier"
		ethirdparty.siret = siret

	ethirdparty.party_name = data.get("party_name", "")
	ethirdparty.address_line1 = data.get("address_line1", "")
	ethirdparty.zip = data.get("zip", "")
	ethirdparty.city = data.get("city", "")
	ethirdparty.country_code = data.get("country_code", "")

	mandatory_custom_fields = frappe.get_all(
		"Custom Field",
		filters={"dt": "Supplier", "reqd": 1},
		pluck="fieldname",
	)
	for fieldname in mandatory_custom_fields:
		if fieldname in data:
			ethirdparty.set(fieldname, data[fieldname])

	if "erpnext_france" in frappe.get_installed_apps():
		for fieldname in ("siret", "siren", "code_naf", "legal_form"):
			value = data.get(fieldname)
			if value:
				ethirdparty.set(fieldname, value)

	ethirdparty.status = "ready"
	ethirdparty.save(ignore_permissions=True)
	frappe.db.commit()

	doc.db_set("ethirdparty", ethirdparty.name)
	frappe.db.set_value(
		"ePurchase Invoice",
		invoice_name,
		{
			"sirene_status": "ok",
			"supplier_match_status": "ethirdparty",
		},
	)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)

	from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import _auto_match_items

	_auto_match_items(doc)

	if apply_to_all and siret:
		others = frappe.get_all(
			"ePurchase Invoice",
			filters={
				"supplier_siret": siret,
				"name": ["!=", invoice_name],
				"conversion_status": ["!=", "converted"],
			},
			pluck="name",
		)
		for other in others:
			frappe.db.set_value(
				"ePurchase Invoice",
				other,
				{
					"ethirdparty": ethirdparty.name,
					"sirene_status": "ok",
				},
			)

	frappe.db.commit()
	doc.reload()
	matched_count = sum(1 for item in doc.items if item.match_status == "matched")
	return {
		"status": "ok",
		"supplier": ethirdparty.party_name,
		"matched_items": matched_count,
		"total_items": len(doc.items),
		"conversion_status": doc.conversion_status,
		"ethirdparty": ethirdparty.name,
	}


@frappe.whitelist()
def unlink_ethirdparty(name, apply_to_all=0):
	apply_to_all = frappe.utils.cint(apply_to_all)
	doc = frappe.get_doc("ePurchase Invoice", name)
	ethirdparty_name = doc.ethirdparty
	siret = doc.supplier_siret

	if ethirdparty_name:
		frappe.db.set_value(
			"ePurchase Invoice",
			{"ethirdparty": ethirdparty_name},
			{"ethirdparty": None, "supplier_match_status": "unmatched", "sirene_status": None},
		)
		frappe.delete_doc("eThirdParty", ethirdparty_name, ignore_permissions=True)

	if apply_to_all and siret:
		frappe.db.set_value(
			"ePurchase Invoice",
			{"supplier_siret": siret, "conversion_status": ["!=", "converted"]},
			{"ethirdparty": None, "supplier_match_status": "unmatched", "sirene_status": None},
		)

	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def update_ethirdparty(name, data):
	if isinstance(data, str):
		import json

		data = json.loads(data)
	doc = frappe.get_doc("eThirdParty", name)
	for field, value in data.items():
		doc.set(field, value)
	doc.status = "ready"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def update_item_tax_rate(name, item_idx, account_head, apply_to_all=0):
	apply_to_all = frappe.utils.cint(apply_to_all)
	item_idx = int(item_idx)
	if not account_head:
		return {"status": "error", "error": "No account selected"}
	tax_rate = float(frappe.db.get_value("Account", account_head, "tax_rate") or 0)
	doc = frappe.get_doc("ePurchase Invoice", name)
	for item in doc.items:
		if item.idx == item_idx:
			item.tax_rate = tax_rate
			item.tax_account_head = account_head
			break
	doc.save(ignore_permissions=True)

	if apply_to_all:
		supplier = doc.matched_supplier
		if supplier:
			all_invoices = frappe.get_all(
				"ePurchase Invoice",
				filters={
					"matched_supplier": supplier,
					"conversion_status": ["!=", "converted"],
				},
				pluck="name",
			)
			other_items = frappe.get_all(
				"ePurchase Invoice Item",
				filters={
					"tax_rate": tax_rate,
					"parent": ["in", all_invoices],
				},
				fields=["name"],
			)
			for row in other_items:
				frappe.db.set_value(
					"ePurchase Invoice Item",
					row.name,
					{
						"tax_account_head": account_head,
					},
				)

	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def count_similar_tax_items(name, tax_rate):
	supplier = frappe.db.get_value("ePurchase Invoice", name, "matched_supplier")
	if not supplier:
		return {"count": 0}
	invoices = frappe.get_all(
		"ePurchase Invoice",
		filters={
			"matched_supplier": supplier,
			"conversion_status": ["!=", "converted"],
		},
		pluck="name",
	)
	if not invoices:
		return {"count": 0}
	count = frappe.db.count(
		"ePurchase Invoice Item",
		{
			"parent": ["in", invoices],
			"tax_rate": float(tax_rate),
		},
	)
	return {"count": count}


@frappe.whitelist()
def match_item(name, item_idx, matched_item, apply_to_all=0):
	apply_to_all = frappe.utils.cint(apply_to_all)
	item_idx = int(item_idx)
	doc = frappe.get_doc("ePurchase Invoice", name)

	ref_raw = None
	for item in doc.items:
		if item.idx == item_idx:
			item.matched_item = matched_item
			item.match_status = "matched"
			ref_raw = item.item_ref_raw
			break

	if apply_to_all and ref_raw:
		for item in doc.items:
			if item.item_ref_raw == ref_raw and item.match_status == "unmatched":
				item.matched_item = matched_item
				item.match_status = "matched"

	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)

	if apply_to_all and ref_raw:
		_apply_item_match_to_all(name, ref_raw, matched_item)

	frappe.db.commit()
	return {"status": "ok"}


def _apply_item_match_to_all(exclude_name, item_ref_raw, matched_item):
	"""Applique le match item à toutes les lignes avec la même référence fournisseur."""
	invoices = frappe.get_all(
		"ePurchase Invoice",
		filters={"name": ["!=", exclude_name], "conversion_status": ["!=", "converted"]},
		pluck="name",
	)
	for inv_name in invoices:
		inv = frappe.get_doc("ePurchase Invoice", inv_name)
		updated = False
		for item in inv.items:
			if item.item_ref_raw == item_ref_raw and item.match_status == "unmatched":
				item.matched_item = matched_item
				item.match_status = "matched"
				updated = True
		if updated:
			inv.save(ignore_permissions=True)


@frappe.whitelist()
def rematch_items(name):
	from erpnext_einvoicing.erpnext_einvoicing.utils.facturx import _auto_match_items

	doc = frappe.get_doc("ePurchase Invoice", name)
	_auto_match_items(doc)
	return {"status": "ok"}


@frappe.whitelist()
def unlink_matched_item(name, item_idx, apply_to_all=0):
	apply_to_all = frappe.utils.cint(apply_to_all)
	item_idx = int(item_idx)
	doc = frappe.get_doc("ePurchase Invoice", name)

	ref_raw = None
	for item in doc.items:
		if item.idx == item_idx:
			ref_raw = item.item_ref_raw
			item.matched_item = None
			item.match_status = "unmatched"
			break

	if apply_to_all and ref_raw:
		for item in doc.items:
			if item.item_ref_raw == ref_raw and item.match_status == "matched":
				item.matched_item = None
				item.match_status = "unmatched"

	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)

	if apply_to_all and ref_raw:
		invoices = frappe.get_all(
			"ePurchase Invoice",
			filters={"name": ["!=", name], "conversion_status": ["!=", "converted"]},
			pluck="name",
		)
		for inv_name in invoices:
			inv = frappe.get_doc("ePurchase Invoice", inv_name)
			updated = False
			for item in inv.items:
				if item.item_ref_raw == ref_raw and item.match_status == "matched":
					item.matched_item = None
					item.match_status = "unmatched"
					updated = True
			if updated:
				inv.save(ignore_permissions=True)
				inv.reload()
				inv._update_conversion_status()
				inv.db_set("conversion_status", inv.conversion_status)

	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def create_item(name, item_idx, item_name, item_group):
	item_idx = int(item_idx)
	doc = frappe.get_doc("ePurchase Invoice", name)

	new_item = frappe.new_doc("Item")
	new_item.item_name = item_name
	new_item.item_group = item_group
	new_item.item_code = item_name
	new_item.is_purchase_item = 1
	new_item.einvoice_source = name
	new_item.insert(ignore_permissions=True)
	frappe.db.commit()

	for item in doc.items:
		if item.idx == item_idx:
			item.matched_item = new_item.name
			item.match_status = "created"
			break
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {"status": "ok", "item": new_item.name}


@frappe.whitelist()
def convert_to_purchase_invoice(name):
	doc = frappe.get_doc("ePurchase Invoice", name)
	return doc.convert_to_purchase_invoice()


@frappe.whitelist()
def convert_all_ready(names):
	import json

	if isinstance(names, str):
		names = json.loads(names)

	converted = 0
	errors = []

	for name in names:
		try:
			doc = frappe.get_doc("ePurchase Invoice", name)
			if doc.conversion_status != "ready":
				continue
			doc.convert_to_purchase_invoice()
			converted += 1
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"eInvoicing convert all - {name}")
			errors.append(name)

	frappe.db.commit()
	return {
		"status": "ok" if not errors else "partial",
		"message": frappe._("{0} converted, {1} errors.").format(converted, len(errors)),
		"converted": converted,
		"errors": errors,
	}


@frappe.whitelist()
def cancel_conversion(name):
	doc = frappe.get_doc("ePurchase Invoice", name)
	if not doc.purchase_invoice:
		return {"status": "error", "error": frappe._("No Purchase Invoice linked.")}

	pi = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
	if pi.docstatus != 0:
		return {
			"status": "error",
			"error": frappe._("Purchase Invoice is already submitted. Cancel it manually first."),
		}

	pi_name = doc.purchase_invoice

	# Délier d'abord l'ePurchase Invoice
	frappe.db.set_value(
		"ePurchase Invoice",
		name,
		{
			"purchase_invoice": None,
			"conversion_status": "ready",
		},
	)
	frappe.db.set_value("Purchase Invoice", pi_name, "einvoice_source", None)
	frappe.db.commit()

	frappe.delete_doc("Purchase Invoice", pi_name, ignore_permissions=True)
	frappe.db.commit()

	return {"status": "ok"}


@frappe.whitelist()
def get_refusal_reasons():
	return frappe.get_all(
		"eInvoicing Refusal Reason",
		fields=["reason_code", "reason_label"],
		order_by="reason_code asc",
	)


@frappe.whitelist()
def refuse_invoice(name, reason_code, reason_comment=None):
	doc = frappe.get_doc("ePurchase Invoice", name)
	if doc.conversion_status in ("converted", "refused"):
		frappe.throw(frappe._("Cannot refuse this invoice."))

	refusal_reasons = [{"MDT-113": reason_code}]
	if reason_comment:
		refusal_reasons[0]["MDT-126"] = reason_comment

	try:
		einvoice = frappe.get_doc("ePurchase Invoice", name)
		_get_provider(einvoice.company).send_lifecycle("210", doc, refusal_reasons)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"eInvoicing lifecycle 210 - {name}")
		frappe.throw(frappe._("Failed to send refusal to platform."))

	frappe.db.set_value("ePurchase Invoice", name, "conversion_status", "refused")
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def get_pi_lifecycle_last_status(pi_name):
	einvoice_name = frappe.db.get_value("ePurchase Invoice", {"purchase_invoice": pi_name}, "name")
	if not einvoice_name:
		return {"status_code": None, "einvoice_name": None}
	last_log = frappe.get_all(
		"eInvoicing Lifecycle Log",
		filters={"parent": einvoice_name},
		fields=["name", "status_code", "status_label", "ack_status", "error_type"],
		order_by="sent_at desc",
		limit=1,
	)
	if not last_log:
		return {
			"status_code": None,
			"ack_status": None,
			"status_label": None,
			"effective_status": None,
			"einvoice_name": einvoice_name,
		}
	log = last_log[0]
	effective_log = frappe.get_all(
		"eInvoicing Lifecycle Log",
		filters={"parent": einvoice_name, "ack_status": ["!=", "error"]},
		fields=["status_code"],
		order_by="sent_at desc",
		limit=1,
	)
	effective_status = effective_log[0]["status_code"] if effective_log else None
	return {
		"status_code": log["status_code"],
		"status_label": log["status_label"],
		"ack_status": log["ack_status"],
		"error_type": log.get("error_type"),
		"log_name": log["name"],
		"effective_status": effective_status,
		"einvoice_name": einvoice_name,
	}


@frappe.whitelist()
def send_invoice_dispute(pi_name, reason_code, reason_comment=None):
	einvoice_name = frappe.db.get_value("ePurchase Invoice", {"purchase_invoice": pi_name}, "name")
	if not einvoice_name:
		frappe.throw(frappe._("No ePurchase Invoice linked to {0}.").format(pi_name))
	last_log = frappe.get_all(
		"eInvoicing Lifecycle Log",
		filters={"parent": einvoice_name},
		fields=["status_code", "ack_status"],
		order_by="sent_at desc",
		limit=1,
	)
	last_status = last_log[0]["status_code"] if last_log else None
	last_ack = last_log[0]["ack_status"] if last_log else None
	if last_status in ("207", "208") and last_ack != "error":
		frappe.throw(frappe._("Cannot send dispute: current status is already {0}.").format(last_status))
	refusal_reasons = [{"MDT-113": reason_code}]
	if reason_comment:
		refusal_reasons[0]["MDT-126"] = reason_comment
	einvoice = frappe.get_doc("ePurchase Invoice", einvoice_name)
	try:
		_get_provider(einvoice.company).send_lifecycle("207", einvoice, refusal_reasons)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"eInvoicing lifecycle 207 - PI {pi_name}")
		frappe.throw(frappe._("Failed to send dispute to platform."))
	return {"status": "ok"}


@frappe.whitelist()
def send_dispute_resolved(pi_name):
	einvoice_name = frappe.db.get_value("ePurchase Invoice", {"purchase_invoice": pi_name}, "name")
	if not einvoice_name:
		frappe.throw(frappe._("No ePurchase Invoice linked to {0}.").format(pi_name))
	logs = frappe.get_all(
		"eInvoicing Lifecycle Log",
		filters={"parent": einvoice_name},
		fields=["status_code"],
		order_by="sent_at desc",
	)
	last_status = logs[0]["status_code"] if logs else None
	if last_status != "207":
		frappe.throw(frappe._("Cannot resolve dispute: current status is not a dispute."))
	status_before_dispute = next(
		(l["status_code"] for l in logs[1:] if l["status_code"] != "207"),
		"205",
	)
	einvoice = frappe.get_doc("ePurchase Invoice", einvoice_name)
	try:
		_get_provider(einvoice.company).send_lifecycle(status_before_dispute, einvoice)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"eInvoicing lifecycle resolve - PI {pi_name}")
		frappe.throw(frappe._("Failed to send dispute resolution to platform."))
	return {"status": "ok"}


@frappe.whitelist()
def send_invoice_suspend(pi_name, reason_code, reason_comment=None):
	einvoice_name = frappe.db.get_value("ePurchase Invoice", {"purchase_invoice": pi_name}, "name")
	if not einvoice_name:
		frappe.throw(frappe._("No ePurchase Invoice linked to {0}.").format(pi_name))

	last_log = frappe.get_all(
		"eInvoicing Lifecycle Log",
		filters={"parent": einvoice_name},
		fields=["status_code", "ack_status"],
		order_by="sent_at desc",
		limit=1,
	)
	last_status = last_log[0]["status_code"] if last_log else None
	last_ack = last_log[0]["ack_status"] if last_log else None
	if last_status == "208" and last_ack != "error":
		frappe.throw(frappe._("Invoice is already suspended."))
	refusal_reasons = [{"MDT-113": reason_code}]
	if reason_comment:
		refusal_reasons[0]["MDT-126"] = reason_comment
	einvoice = frappe.get_doc("ePurchase Invoice", einvoice_name)
	try:
		_get_provider(einvoice.company).send_lifecycle("208", einvoice, refusal_reasons)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"eInvoicing lifecycle 208 - PI {pi_name}")
		frappe.throw(frappe._("Failed to send suspension to platform."))
	return {"status": "ok"}


@frappe.whitelist()
def retry_lifecycle(pi_name):
	einvoice_name = frappe.db.get_value("ePurchase Invoice", {"purchase_invoice": pi_name}, "name")
	if not einvoice_name:
		frappe.throw(frappe._("No ePurchase Invoice linked to {0}.").format(pi_name))
	last_log = frappe.get_all(
		"eInvoicing Lifecycle Log",
		filters={"parent": einvoice_name},
		fields=["status_code"],
		order_by="sent_at desc",
		limit=1,
	)
	if not last_log:
		frappe.throw(frappe._("No lifecycle log found."))
	status_code = last_log[0]["status_code"]
	if status_code in ("207", "208", "210"):
		frappe.throw(frappe._("Use the eInvoicing buttons to retry this status."))
	einvoice = frappe.get_doc("ePurchase Invoice", einvoice_name)
	try:
		_get_provider(einvoice.company).send_lifecycle(status_code, einvoice)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"eInvoicing lifecycle retry - PI {pi_name}")
		frappe.throw(frappe._("Failed to retry lifecycle status."))
	return {"status": "ok"}


### Scheduled task


def sync_incoming_flows():
	"""Called by scheduler every 30 min (see hooks.py)."""
	companies = frappe.get_all(
		"Company",
		filters={
			"einvoicing_approved_platform": ["!=", ""],
			"einvoicing_auto_sync": 1,
		},
		pluck="name",
	)
	for company in companies:
		try:
			result = _get_provider(company).sync_flows("Purchase Invoice")
			if result.get("status") == "error":
				frappe.log_error(result.get("message"), f"eInvoicing scheduled sync error - {company}")
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"eInvoicing scheduled sync exception - {company}")


@frappe.whitelist()
def poll_lifecycle_acknowledgements():
	"""Called by scheduler"""
	try:
		pending_logs = frappe.get_all(
			"eInvoicing Lifecycle Log",
			filters={"ack_status": "pending"},
			fields=["name", "cdar_flow_id", "parent"],
		)
		for log in pending_logs:
			company = frappe.db.get_value("ePurchase Invoice", log.parent, "company")
			_poll_one_lifecycle_log(_get_provider(company), log)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "eInvoicing lifecycle poll exception")


def _poll_one_lifecycle_log(provider, log):
	try:
		if not log.cdar_flow_id:
			frappe.db.set_value(
				"eInvoicing Lifecycle Log",
				log.name,
				{
					"ack_status": "error",
					"error_type": "data",
					"ack_message": frappe._("No flow ID - CDAR was not sent successfully"),
				},
			)
			return

		result = provider.call_api(
			f"flows/{log.cdar_flow_id}",
			"GET",
			params={"docType": "Metadata"},
			extra_headers={"Accept": "application/octet-stream"},
		)
		if result["status_code"] not in (200, 202):
			frappe.db.set_value(
				"eInvoicing Lifecycle Log",
				log.name,
				{
					"ack_status": "error",
					"error_type": "platform",
					"ack_message": frappe._("HTTP {0}").format(result["status_code"]),
				},
			)
			return

		ack = result["response"].get("acknowledgement", {})
		ack_status = ack.get("status", "")

		if ack_status == "Ok":
			frappe.db.set_value(
				"eInvoicing Lifecycle Log",
				log.name,
				{"ack_status": "ok", "error_type": None, "ack_message": None},
			)
		elif ack_status == "Error":
			details = ack.get("details", [])
			msg = details[0].get("reasonMessage", "") if details else ""
			frappe.db.set_value(
				"eInvoicing Lifecycle Log",
				log.name,
				{"ack_status": "error", "error_type": "data", "ack_message": msg},
			)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"eInvoicing lifecycle poll - {log.name}")


@frappe.whitelist()
def poll_single_lifecycle_log(log_name):
	provider = _get_provider()
	log = frappe.get_all(
		"eInvoicing Lifecycle Log",
		filters={"name": log_name},
		fields=["name", "cdar_flow_id", "parent"],
		limit=1,
	)
	if not log:
		return {"status": "error"}
	_poll_one_lifecycle_log(provider, log[0])
	return {"status": "ok"}


@frappe.whitelist()
def rebuild_lifecycle_logs():
	from frappe.utils import get_datetime
	from pyfrctc import parse_cdar

	from erpnext_einvoicing.providers.base_provider import LIFECYCLE_STATUS_MAP

	frappe.only_for("System Manager")
	provider = _get_provider()

	# Factures sans log indexées par invoice_number
	invoices_without_log = {
		inv["invoice_number"]: inv["name"]
		for inv in frappe.db.sql(
			"""
            SELECT name, invoice_number
            FROM `tabePurchase Invoice`
            WHERE name NOT IN (SELECT DISTINCT parent
                               FROM `tabeInvoicing Lifecycle Log`)
              AND invoice_number IS NOT NULL
              AND invoice_number != ''
			""",
			as_dict=True,
		)
	}

	if not invoices_without_log:
		return {"status": "ok", "message": frappe._("No invoices to process.")}

	# Un seul appel pour tous les CDARs sortants
	result = provider.call_api(
		"flows/search",
		"POST",
		params={
			"where": {
				"updatedAfter": "1970-01-01T00:00:00.000Z",
				"flowDirection": ["Out"],
			},
			"limit": 1000,
		},
	)
	if result["status_code"] not in (200, 202):
		return {"status": "error", "message": f"HTTP {result['status_code']}"}

	flows = [
		f
		for f in result["response"].get("results", [])
		if f.get("flowSyntax") == "CDAR" and f.get("flowType") == "SupplierInvoiceLC"
	]

	if not flows:
		return {"status": "ok", "message": frappe._("No CDAR flows found.")}

	created = skipped = errors = 0

	for flow in flows:
		try:
			flow_id = flow.get("flowId")

			xml_result = provider.call_api(
				f"flows/{flow_id}",
				"GET",
				params={"docType": "Original"},
				extra_headers={"Accept": "application/octet-stream"},
			)

			if xml_result["status_code"] not in (200, 202):
				errors += 1
				continue
			if not isinstance(xml_result["response"], bytes):
				errors += 1
				continue

			cdar_dict = parse_cdar(xml_result["response"])
			invoice_number = cdar_dict.get("invoice_number", "")
			status_code = cdar_dict.get("status_code", "")

			if not invoice_number or not status_code:
				skipped += 1
				continue

			einvoice_name = invoices_without_log.get(invoice_number)
			if not einvoice_name:
				skipped += 1
				continue

			if frappe.db.exists("eInvoicing Lifecycle Log", {"cdar_flow_id": flow_id}):
				skipped += 1
				continue

			ack = flow.get("acknowledgement", {})
			ack_status_raw = ack.get("status", "")
			ack_status = (
				"ok" if ack_status_raw == "Ok" else ("error" if ack_status_raw == "Error" else "pending")
			)
			ack_message = (ack.get("details") or [{}])[0].get("reasonMessage", "") or None

			log = frappe.new_doc("eInvoicing Lifecycle Log")
			log.parent = einvoice_name
			log.parenttype = "ePurchase Invoice"
			log.parentfield = "lifecycle_logs"
			log.status_code = status_code
			log.status_label = frappe._(LIFECYCLE_STATUS_MAP.get(status_code, status_code))
			log.cdar_flow_id = flow_id
			submitted_at = flow.get("submittedAt", "")
			if submitted_at:
				dt = get_datetime(submitted_at)
				log.sent_at = dt.replace(tzinfo=None) if dt else None

			log.ack_status = ack_status
			log.error_type = "data" if ack_status == "error" else None
			log.ack_message = ack_message
			log.insert(ignore_permissions=True)
			created += 1

		except Exception:
			import traceback

			frappe.log_error(frappe.get_traceback(), f"rebuild_lifecycle_logs - {flow.get('flowId')}")
			errors += 1

	frappe.db.commit()
	return {
		"status": "ok",
		"message": frappe._("{0} logs created, {1} skipped, {2} errors.").format(created, skipped, errors),
	}


@frappe.whitelist()
def send_lifecycle_status(name, status_code, refusal_reasons=None):
	if isinstance(refusal_reasons, str):
		import json as _json

		refusal_reasons = _json.loads(refusal_reasons) if refusal_reasons else None
	doc = frappe.get_doc("ePurchase Invoice", name)
	return _get_provider(doc.company).send_lifecycle(status_code, doc, refusal_reasons)


@frappe.whitelist()
def get_po_candidates(name, item_idx):
	doc = frappe.get_doc("ePurchase Invoice", name)
	if not doc.matched_supplier:
		return []
	item_idx = int(item_idx)
	item = next((i for i in doc.items if i.idx == item_idx), None)
	if not item or not item.matched_item:
		return []

	open_pos = frappe.get_all(
		"Purchase Order",
		filters={
			"supplier": doc.matched_supplier,
			"status": ["in", ["To Receive and Bill", "To Bill", "Partly Billed"]],
			"docstatus": 1,
		},
		pluck="name",
	)
	if not open_pos:
		return []
	lines = frappe.get_all(
		"Purchase Order Item",
		filters={"item_code": item.matched_item, "parent": ["in", open_pos]},
		fields=["name", "parent", "item_name", "qty"],
	)
	result = []
	for l in lines:
		billed_qty = frappe.db.sql(
			"""
            SELECT COALESCE(SUM(qty), 0)
            FROM `tabPurchase Invoice Item`
            WHERE po_detail = %s
              AND docstatus = 1
			""",
			l.name,
		)[0][0]
		remaining_qty = round(l.qty - billed_qty, 3)
		if remaining_qty > 0:
			result.append(
				{
					"name": l.name,
					"parent": l.parent,
					"item_name": l.item_name,
					"qty": l.qty,
					"remaining_qty": remaining_qty,
				}
			)
	return result


@frappe.whitelist()
def get_pr_candidates(name, item_idx):
	doc = frappe.get_doc("ePurchase Invoice", name)
	if not doc.matched_supplier:
		return []
	item_idx = int(item_idx)
	item = next((i for i in doc.items if i.idx == item_idx), None)
	if not item or not item.matched_item:
		return []
	open_prs = frappe.get_all(
		"Purchase Receipt",
		filters={
			"supplier": doc.matched_supplier,
			"status": ["in", ["To Bill", "Partly Billed"]],
			"docstatus": 1,
		},
		pluck="name",
	)
	if not open_prs:
		return []
	lines = frappe.get_all(
		"Purchase Receipt Item",
		filters={"item_code": item.matched_item, "parent": ["in", open_prs]},
		fields=["name", "parent", "item_name", "qty"],
	)
	result = []
	for l in lines:
		billed_qty = frappe.db.sql(
			"""
            SELECT COALESCE(SUM(qty), 0)
            FROM `tabPurchase Invoice Item`
            WHERE pr_detail = %s
              AND docstatus = 1
			""",
			l.name,
		)[0][0]
		remaining_qty = round(l.qty - billed_qty, 3)
		if remaining_qty > 0:
			result.append(
				{
					"name": l.name,
					"parent": l.parent,
					"item_name": l.item_name,
					"qty": l.qty,
					"remaining_qty": remaining_qty,
				}
			)
	return result


@frappe.whitelist()
def match_item_po(name, item_idx, purchase_order, po_detail):
	item_idx = int(item_idx)
	doc = frappe.get_doc("ePurchase Invoice", name)
	for item in doc.items:
		if item.idx == item_idx:
			po_qty = frappe.db.get_value("Purchase Order Item", po_detail, "qty")
			billed_qty = frappe.db.sql(
				"""
                SELECT COALESCE(SUM(qty), 0)
                FROM `tabPurchase Invoice Item`
                WHERE po_detail = %s
                  AND docstatus = 1
				""",
				po_detail,
			)[0][0]
			remaining = po_qty - billed_qty
			item.purchase_order = purchase_order
			item.po_detail = po_detail
			item.po_match_status = "matched" if remaining == item.qty else "partial"
			break
	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def match_item_pr(name, item_idx, purchase_receipt, pr_detail):
	item_idx = int(item_idx)
	doc = frappe.get_doc("ePurchase Invoice", name)
	for item in doc.items:
		if item.idx == item_idx:
			pr_qty = frappe.db.get_value("Purchase Receipt Item", pr_detail, "qty")
			billed_qty = frappe.db.sql(
				"""
                SELECT COALESCE(SUM(qty), 0)
                FROM `tabPurchase Invoice Item`
                WHERE pr_detail = %s
                  AND docstatus = 1
				""",
				pr_detail,
			)[0][0]
			remaining = pr_qty - billed_qty
			item.purchase_receipt = purchase_receipt
			item.pr_detail = pr_detail
			item.pr_match_status = "matched" if remaining == item.qty else "partial"
			break
	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def unlink_item_po(name, item_idx):
	item_idx = int(item_idx)
	doc = frappe.get_doc("ePurchase Invoice", name)
	for item in doc.items:
		if item.idx == item_idx:
			item.purchase_order = None
			item.po_detail = None
			item.po_match_status = None
			break
	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def unlink_item_pr(name, item_idx):
	item_idx = int(item_idx)
	doc = frappe.get_doc("ePurchase Invoice", name)
	for item in doc.items:
		if item.idx == item_idx:
			item.purchase_receipt = None
			item.pr_detail = None
			item.pr_match_status = None
			break
	doc.save(ignore_permissions=True)
	doc.reload()
	doc._update_conversion_status()
	doc.db_set("conversion_status", doc.conversion_status)
	frappe.db.commit()
	return {"status": "ok"}


@frappe.whitelist()
def get_credit_note_status(name):
	doc = frappe.get_doc("ePurchase Invoice", name)
	if not doc.is_credit_note:
		return {"is_credit_note": 0}
	ref_status = None
	if doc.referenced_epurchase_invoice:
		ref_status = frappe.db.get_value(
			"ePurchase Invoice",
			doc.referenced_epurchase_invoice,
			"conversion_status",
		)
	return {
		"is_credit_note": 1,
		"referenced_invoice_number": doc.referenced_invoice_number,
		"referenced_epurchase_invoice": doc.referenced_epurchase_invoice,
		"ref_status": ref_status,
	}
