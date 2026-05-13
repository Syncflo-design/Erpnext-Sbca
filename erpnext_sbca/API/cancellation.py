# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""Cancel Sales Orders and Purchase Orders in Sage when cancelled in ERPNext.

Triggered by:
  - doc_events["Sales Order"]["on_cancel"]
  - doc_events["Purchase Order"]["on_cancel"]

Gated by the existing push toggles (push_sales_order_on_submit /
push_purchase_order_on_submit). If the push is disabled, cancels are also
skipped — consistent with "this company isn't syncing to Sage".

Identification: uses custom_sage_document_number (the human-readable Sage
document number stamped on the original push). If the field is empty the
document was never successfully pushed to Sage, so the cancel is silent.

Pharoh endpoints (to be built — see Pharoh_Cancellation_Endpoint_Prompt.txt):
  POST /api/SalesOrder/cancel-salesorder-in-sage
  POST /api/PurchaseOrder/cancel-purchaseorder-in-sage

Both endpoints receive credentials + documentNumber and call Sage's
  POST SalesOrder/Save?useSystemDocumentNumber=false  (or PurchaseOrder/Save...)
internally, setting the document status to Cancelled.

No un-cancel feature — once cancelled in Sage, the document stays cancelled.
If a mistaken cancel needs reverting, it must be done directly in Sage.
"""

import frappe
from frappe.integrations.utils import make_post_request

url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled


# ---------------------------------------------------------------------------
# Doc event wrappers
# ---------------------------------------------------------------------------

def cancel_sales_order(doc, method):
    """on_cancel handler for Sales Order.

    Silently skips if the push toggle is off or if the order was never
    successfully synced to Sage (custom_sage_document_number is empty).
    """
    if not is_sync_enabled("push_sales_order_on_submit"):
        return
    if not doc.get("custom_sage_document_number"):
        return  # Never reached Sage — nothing to cancel.

    frappe.enqueue(
        "erpnext_sbca.API.cancellation._cancel_worker",
        queue="default",
        timeout=300,
        enqueue_after_commit=True,
        doctype="Sales Order",
        doc_name=doc.name,
        pharoh_path="SalesOrder/cancel-salesorder-in-sage",
    )


def cancel_purchase_order(doc, method):
    """on_cancel handler for Purchase Order.

    Silently skips if the push toggle is off or if the order was never
    successfully synced to Sage (custom_sage_document_number is empty).
    """
    if not is_sync_enabled("push_purchase_order_on_submit"):
        return
    if not doc.get("custom_sage_document_number"):
        return  # Never reached Sage — nothing to cancel.

    frappe.enqueue(
        "erpnext_sbca.API.cancellation._cancel_worker",
        queue="default",
        timeout=300,
        enqueue_after_commit=True,
        doctype="Purchase Order",
        doc_name=doc.name,
        pharoh_path="PurchaseOrder/cancel-purchaseorder-in-sage",
    )


# ---------------------------------------------------------------------------
# Shared worker
# ---------------------------------------------------------------------------

def _cancel_worker(doctype, doc_name, pharoh_path):
    """Build the cancellation payload and POST to Pharoh.

    Uses custom_sage_document_number as the Sage-side identifier. Matches
    the document's company to the correct Company Sage Integration row —
    same single-company pattern as stock_adjustment.py, avoiding the
    cross-posting bug in the POS invoice handler.

    On success: stamps custom_sage_sync_status = "Cancelled".
    On failure: stamps "Cancel Failed" and logs the error.
    """
    doc = frappe.get_doc(doctype, doc_name)
    document_number = doc.get("custom_sage_document_number")

    if not document_number:
        # Defensive guard — wrapper already checks, but be safe on retries.
        return

    company = doc.company
    settings = frappe.get_doc("Erpnext Sbca Settings")
    integration_name = frappe.db.get_value(
        "Company Sage Integration",
        {"parent": settings.name, "company": company},
        "name",
    )
    if not integration_name:
        frappe.log_error(
            title=f"Sage Cancel: no integration for '{company}'"[:140],
            message=(
                f"No Company Sage Integration row found for company '{company}'.\n"
                f"Document: {doctype} {doc_name} — Sage document number: "
                f"{document_number}.\n"
                f"Add the integration row in Erpnext Sbca Settings → Connection, "
                f"then manually cancel document {document_number} in Sage."
            ),
        )
        return

    integration = frappe.get_doc("Company Sage Integration", integration_name)
    apikey = integration.get_password("api_key")
    if not apikey:
        frappe.log_error(
            title=f"Sage Cancel: missing API key for '{company}'"[:140],
            message=(
                f"API key missing on Company Sage Integration for '{company}'.\n"
                f"Manually cancel document {document_number} in Sage."
            ),
        )
        return

    payload = {
        "credentials": {
            "loginName": integration.username,
            "loginPwd": integration.get_password("password"),
            "useOAuth": bool(integration.use_oauth),
            "sessionToken": integration.get_password("session_id"),
            "provider": integration.get_password("provider"),
        },
        "documentNumber": document_number,
    }

    endpoint_url = f"{url}/api/{pharoh_path}?apikey={apikey}"

    try:
        response = make_post_request(
            endpoint_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if response and response.get("success"):
            try:
                doc.db_set("custom_sage_sync_status", "Cancelled")
            except Exception:
                pass
        else:
            error_msg = (
                response.get("errorMessage") or str(response)
                if response
                else "No response body from Pharoh."
            )
            frappe.log_error(
                title=f"Sage Cancel Failed — {doc_name}"[:140],
                message=(
                    f"Pharoh returned failure for {doctype} '{doc_name}'.\n"
                    f"Sage document number: {document_number}\n"
                    f"Error: {error_msg}\n"
                    f"Manually cancel this document in Sage."
                ),
            )
            try:
                doc.db_set("custom_sage_sync_status", "Cancel Failed")
            except Exception:
                pass

    except Exception as e:
        frappe.log_error(
            title=f"Sage Cancel HTTP Error — {doc_name}"[:140],
            message=(
                f"HTTP error cancelling {doctype} '{doc_name}' in Sage.\n"
                f"Sage document number: {document_number}\n"
                f"Error: {str(e)}\n"
                f"Manually cancel this document in Sage."
            ),
        )
        try:
            doc.db_set("custom_sage_sync_status", "Cancel Failed")
        except Exception:
            pass
