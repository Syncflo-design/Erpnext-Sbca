# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""One-time stock cutover: Sage -> ERPNext.

The customer was running on Sage before going live on ERPNext, so Sage holds
the current on-hand quantities. This module migrates those levels into ERPNext
(once, per Company), then lets the user tell Sage to stop tracking quantities
- handing stock authority entirely to ERPNext.

Two user-triggered steps, both on the Stock tab of Erpnext Sbca Settings, both
operating on the Active Company:

  1. Import Stock Levels
     Pulls Sage's on-hand quantities + unit costs, creates ONE submitted
     Stock Reconciliation (purpose "Opening Stock") into the Company's Default
     Warehouse, and sets stock_import_complete = 1. From that point the
     scheduled qty-on-hand pull skips this Company (see item_details.py) so
     Sage can never overwrite ERPNext's levels.

  2. Disable Sage Qty Tracking
     Gated on stock_import_complete = 1 - you can't hand stock authority to
     ERPNext until ERPNext actually has the levels. Sends the Company's
     Sage-managed physical item codes to Pharoh, which disables qty tracking
     per item in Sage. Sets sage_qty_tracking_disabled = 1.

There is no reset path by design: a botched first attempt is recovered by
re-running on a fresh sandbox instance, not by un-setting the flag.

PHAROH ENDPOINTS (live on the merged InventorySyncController - the shapes below
are what this code expects):

  POST /api/InventorySync/get-stock-levels-for-erpnext?apikey=...&skipQty=...
    body:    {loginName, loginPwd, useOAuth, sessionToken, provider}
    returns: a paginated envelope
             {"totalResults": <int>, "returnedResults": <int>,
              "items": [{"item_code": "...", "quantity": <num>,
                         "valuation_rate": <num>}, ...]}
             valuation_rate is the Sage unit cost - required so ERPNext values
             the opening stock correctly and the GL posts the right amount.
             fetch_all_pages() drives the skipQty loop and returns the
             combined items list.

  POST /api/InventorySync/disable-qty-tracking?apikey=...
    body:    {"credentials": {...}, "itemCodes": ["...", ...]}
    returns: {"success": true, "disabled": <int>, "errors": [<str>, ...]}
"""

import frappe
from frappe.integrations.utils import make_post_request
from erpnext_sbca.API.helper_function import fetch_all_pages

url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_integration(company):
    """Return the Company Sage Integration doc for a Company, or None."""
    name = frappe.db.get_value(
        "Company Sage Integration",
        {"parent": "Erpnext Sbca Settings", "company": company},
        "name",
    )
    return frappe.get_doc("Company Sage Integration", name) if name else None


def _credentials(integration):
    """Standard Sage credential block used by the Pharoh stock endpoints."""
    return {
        "loginName": integration.username,
        "loginPwd": integration.get_password("password"),
        "useOAuth": bool(integration.use_oauth),
        "sessionToken": integration.get_password("session_id"),
        "provider": integration.get_password("provider"),
    }


# ---------------------------------------------------------------------------
# Active Company link filter - Companies eligible for the Stock setup workflow
# ---------------------------------------------------------------------------

@frappe.whitelist()
def companies_ready_for_stock_query(doctype, txt, searchfield, start, page_len, filters):
    """Link-field set_query handler for the Stock tab's Active Company picker.

    A Company is eligible when it has a Company Sage Integration row WITH a
    Default Warehouse set - the minimum needed to land an Opening Stock
    reconciliation somewhere.
    """
    rows = frappe.get_all(
        "Company Sage Integration",
        filters={"parent": "Erpnext Sbca Settings"},
        fields=["company", "default_warehouse"],
    )
    eligible = sorted({r.company for r in rows if r.default_warehouse})
    if txt:
        needle = txt.lower()
        eligible = [c for c in eligible if needle in c.lower()]
    return [[c] for c in eligible]


# ---------------------------------------------------------------------------
# Status - drives the Stock tab banner
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_stock_setup_status(company):
    """Return status flags + counts for the Stock tab banner.

    Keys:
      has_integration            - a Company Sage Integration row exists
      default_warehouse          - the row's Default Warehouse (or None)
      stock_import_complete       - Import Stock Levels has run
      sage_qty_tracking_disabled  - Disable Sage Qty Tracking has run
      stock_item_count            - physical (is_stock_item) items in the system
      cutover_armed               - the Confirm Production Cutover arming switch
      ready_for_import            - has warehouse AND not yet imported
      ready_for_disable           - imported AND armed AND not yet disabled
    """
    if not company:
        return {}

    integration = _get_integration(company)
    if not integration:
        return {
            "has_integration": False,
            "default_warehouse": None,
            "stock_import_complete": False,
            "sage_qty_tracking_disabled": False,
            "cutover_armed": False,
            "stock_item_count": 0,
            "ready_for_import": False,
            "ready_for_disable": False,
        }

    stock_item_count = frappe.db.count(
        "Item", filters={"is_stock_item": 1, "disabled": 0}
    )

    default_warehouse = integration.get("default_warehouse")
    stock_import_complete = bool(integration.get("stock_import_complete"))
    sage_qty_tracking_disabled = bool(integration.get("sage_qty_tracking_disabled"))
    cutover_armed = bool(integration.get("confirm_production_cutover"))

    return {
        "has_integration": True,
        "default_warehouse": default_warehouse,
        "stock_import_complete": stock_import_complete,
        "sage_qty_tracking_disabled": sage_qty_tracking_disabled,
        "cutover_armed": cutover_armed,
        "stock_item_count": stock_item_count,
        "ready_for_import": bool(default_warehouse) and not stock_import_complete,
        "ready_for_disable": (
            stock_import_complete
            and cutover_armed
            and not sage_qty_tracking_disabled
        ),
    }


# ---------------------------------------------------------------------------
# Step 1 - Import Stock Levels
# ---------------------------------------------------------------------------

@frappe.whitelist()
def import_stock_levels_from_sage(company):
    """One-time: pull Sage on-hand levels into a Stock Reconciliation.

    Creates one submitted Stock Reconciliation (purpose "Opening Stock") for
    the Company, landing every physical item's qty + Sage valuation into the
    Company's Default Warehouse, then sets stock_import_complete = 1.

    The "Opening Stock" purpose is deliberately chosen: post_stock_reconciliation
    in stock_adjustment.py skips that purpose, so this import never gets pushed
    back to Sage.

    Refuses if: no integration row, no Default Warehouse, or the import has
    already run for this Company.
    """
    integration = _get_integration(company)
    if not integration:
        frappe.throw(f"No Company Sage Integration row for '{company}'.")

    warehouse = integration.get("default_warehouse")
    if not warehouse:
        frappe.throw(
            f"Set a Default Warehouse on the '{company}' Company Sage "
            f"Integration row (Connection tab) before importing stock levels."
        )

    if integration.get("stock_import_complete"):
        frappe.throw(
            f"Stock has already been imported for '{company}'. The import "
            f"runs once by design. To redo it, use a fresh sandbox instance."
        )

    apikey = integration.get_password("api_key")
    endpoint_url = (
        f"{url}/api/InventorySync/get-stock-levels-for-erpnext?apikey={apikey}"
    )
    # Pharoh paginates this endpoint — fetch_all_pages drives the skipQty
    # loop and returns the combined list of stock-level rows. The whole list
    # is needed up front here: this builds ONE Opening Stock reconciliation.
    levels = fetch_all_pages(endpoint_url, _credentials(integration))

    sr = frappe.new_doc("Stock Reconciliation")
    sr.company = company
    sr.purpose = "Opening Stock"

    added = 0
    skipped_missing = []
    skipped_service = []
    skipped_zero = []
    for row in levels:
        item_code = row.get("item_code")
        if isinstance(item_code, str):
            item_code = item_code.strip()
        if not item_code:
            continue

        qty = row.get("quantity") or 0
        valuation = row.get("valuation_rate") or 0

        if not frappe.db.exists("Item", item_code):
            # Item not yet synced into ERPNext - run the item sync first.
            skipped_missing.append(item_code)
            continue
        if not frappe.db.get_value("Item", item_code, "is_stock_item"):
            # Service / non-stock item - no stock ledger, nothing to import.
            skipped_service.append(item_code)
            continue
        if not qty:
            # Zero on-hand - no opening position to establish.
            skipped_zero.append(item_code)
            continue

        sr.append(
            "items",
            {
                "item_code": item_code,
                "warehouse": warehouse,
                "qty": qty,
                "valuation_rate": valuation,
            },
        )
        added += 1

    if added == 0:
        frappe.throw(
            f"Nothing to import for '{company}'. Sage returned no physical "
            f"items with on-hand quantity that exist in ERPNext "
            f"(missing in ERPNext: {len(skipped_missing)}, "
            f"service items: {len(skipped_service)}, "
            f"zero qty: {len(skipped_zero)}). "
            f"Run the item sync first if items are missing."
        )

    sr.insert(ignore_permissions=True)
    sr.submit()

    frappe.db.set_value(
        "Company Sage Integration",
        integration.name,
        "stock_import_complete",
        1,
    )
    frappe.db.commit()

    frappe.logger("sbca").info(
        f"Stock import for {company}: reconciliation={sr.name}, "
        f"imported={added}, missing={len(skipped_missing)}, "
        f"service={len(skipped_service)}, zero={len(skipped_zero)}"
    )

    return {
        "company": company,
        "reconciliation": sr.name,
        "warehouse": warehouse,
        "imported": added,
        "skipped_missing": skipped_missing,
        "skipped_service": skipped_service,
        "skipped_zero": skipped_zero,
    }


# ---------------------------------------------------------------------------
# Step 2 - Disable Sage Qty Tracking
# ---------------------------------------------------------------------------

@frappe.whitelist()
def disable_sage_qty_tracking(company):
    """Tell Sage to stop tracking quantities for this Company's items.

    DANGEROUS: this permanently changes the REAL Sage company. There is no
    sandbox Sage — sandbox/UAT ERPNext points at the same Sage company as
    production. Three guards stand between a button click and Sage:

      1. stock_import_complete must be 1 — ERPNext must already hold the
         stock before Sage's tracking is switched off.
      2. confirm_production_cutover (the arming switch) must be ticked — a
         deliberate, manual step done ONLY at real go-live, never in sandbox.
      3. sage_qty_tracking_disabled must be 0 — can't run twice.

    Sends the Sage-managed physical item codes to Pharoh, which disables qty
    tracking per item in Sage. Sets sage_qty_tracking_disabled = 1 on success.
    """
    integration = _get_integration(company)
    if not integration:
        frappe.throw(f"No Company Sage Integration row for '{company}'.")

    if not integration.get("stock_import_complete"):
        frappe.throw(
            f"Run Import Stock Levels for '{company}' first. Sage qty "
            f"tracking can't be disabled until ERPNext holds the stock."
        )

    if not integration.get("confirm_production_cutover"):
        frappe.throw(
            f"PRODUCTION CUTOVER NOT ARMED for '{company}'.\n\n"
            f"Disable Sage Qty Tracking permanently changes the REAL Sage "
            f"company — there is no separate sandbox Sage. To unlock it, tick "
            f"'Confirm Production Cutover (Arming Switch)' on the '{company}' "
            f"Company Sage Integration row (Connection tab) and save. Do that "
            f"ONLY at real go-live, never during sandbox or UAT testing."
        )

    if integration.get("sage_qty_tracking_disabled"):
        frappe.throw(
            f"Sage qty tracking is already disabled for '{company}'."
        )

    # Sage-managed physical items - the ones Sage currently tracks qty for.
    item_codes = frappe.get_all(
        "Item",
        filters={
            "is_stock_item": 1,
            "custom_sage_selection_id": ["is", "set"],
        },
        pluck="item_code",
    )
    if not item_codes:
        frappe.throw(
            f"No Sage-managed physical items found for '{company}' - "
            f"nothing to disable tracking for."
        )

    apikey = integration.get_password("api_key")
    endpoint_url = (
        f"{url}/api/InventorySync/disable-qty-tracking?apikey={apikey}"
    )
    payload = {
        "credentials": _credentials(integration),
        "itemCodes": item_codes,
    }
    response = make_post_request(endpoint_url, json=payload)

    if not response or not response.get("success"):
        error_msg = (
            response.get("errorMessage") or str(response)
            if response
            else "Pharoh returned no response body."
        )
        frappe.log_error(
            title=f"Sage Disable Qty Tracking failed for {company}"[:140],
            message=f"Error: {error_msg}\nItems sent: {len(item_codes)}",
        )
        frappe.throw(
            f"Sage disable-qty-tracking failed for '{company}': {error_msg}"
        )

    frappe.db.set_value(
        "Company Sage Integration",
        integration.name,
        "sage_qty_tracking_disabled",
        1,
    )
    frappe.db.commit()

    frappe.logger("sbca").info(
        f"Sage qty tracking disabled for {company}: "
        f"items_sent={len(item_codes)}, disabled={response.get('disabled')}"
    )

    return {
        "company": company,
        "items_sent": len(item_codes),
        "disabled": response.get("disabled", len(item_codes)),
        "errors": response.get("errors", []),
    }
