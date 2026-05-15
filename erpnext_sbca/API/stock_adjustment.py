# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""Push ERPNext stock value movements to Sage as Journal Entries on submit.

Triggered by:
  - doc_events["Stock Entry"]["on_submit"]        — value-affecting types only
  - doc_events["Stock Reconciliation"]["on_submit"]

Gated by the `push_stock_adjustment_on_submit` toggle on Erpnext Sbca Settings.
Defaults to OFF until Pharoh's stock-adjustment endpoint is live.

Pharoh endpoint: POST /api/StockAdjustmentSync/post-stock-adjustment-to-sage

DESIGN PRINCIPLE
----------------
"No change in net stock value to the company = No journal."

Stock Entry types that ARE pushed (value leaves, enters, or transforms):
  Material Issue                      — internal consumption (value out)
  Material Receipt                    — receipt without PO/Invoice (value in)
  Manufacture                         — RM consumed, FG created (transformation)
  Material Consumption for Manufacture — RM consumed for work order
  Repack                              — kit building / component splitting
  Disassemble                         — reverse of kit build / disassembly

Stock Entry types that are NOT pushed (internal moves, no net value change):
  Material Transfer                   — warehouse A → warehouse B
  Material Transfer for Manufacture   — store warehouse → WIP warehouse
  Send to Subcontractor               — still company-owned, just offsite

Stock Reconciliation is always pushed — it explicitly corrects stock value
to match physical reality.

APPROACH — GL Entry reflection
------------------------------
Rather than reconstructing accounting logic per document type, this module reads
ERPNext's GL Entry records for the submitted document. ERPNext has already
computed the exact DR/CR lines, amounts, and accounts. We translate account names
to Sage account IDs (via custom_sage_account_id) and forward to Pharoh.

This single code path covers: physical-count variances, internal consumption,
kit builds, disassembly, raw-material-to-WIP-to-FG manufacturing conversions,
and any other future entry type that generates GL entries.

PAYLOAD SHAPE (what Pharoh must accept)
---------------------------------------
{
  "credentials": {
    "loginName": "...", "loginPwd": "...",
    "useOAuth": false, "sessionToken": "...", "provider": "..."
  },
  "stockAdjustment": {
    "date": "yyyy-MM-dd",
    "reference": "<ERPNext doc name>",
    "entryType": "<stock_entry_type | 'Stock Reconciliation'>",
    "description": "<human readable>",
    "memo": "<human readable>",
    "taxPeriodId": null,
    "analysisCategoryId1": null,
    "analysisCategoryId2": null,
    "analysisCategoryId3": null,
    "trackingCode": "",
    "businessId": null,
    "payRunId": null,
    "lines": [
      {
        "effect": 1,           // 1=Debit, 2=Credit (Sage enum)
        "accountId": <int>,    // custom_sage_account_id on Account
        "debit": <decimal>,
        "credit": <decimal>,
        "exclusive": <decimal>,  // == debit if debit>0, else credit
        "tax": 0,
        "total": <decimal>,
        "taxTypeId": null,
        "description": "<GL entry remarks>"
      },
      ...
    ]
  }
}

Nullable integers (taxTypeId, taxPeriodId, analysisCategoryId1/2/3,
businessId, payRunId) are sent as null — never 0. Sage treats 0 as an
invalid foreign key reference.

PREREQUISITES
-------------
- Account sync must have run so every GL account carries custom_sage_account_id.
- A Company Sage Integration row must exist for the document's company.
- Pharoh's /api/StockAdjustmentSync/post-stock-adjustment-to-sage endpoint
  must be live, returning {"success": true, "sageOrderId": ..., "documentNumber": ...}.
  Until then, leave the push_stock_adjustment_on_submit toggle OFF in Settings.
"""

import frappe
import json
from frappe.integrations.utils import make_post_request

url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled


SAGE_EFFECT_DEBIT = 1
SAGE_EFFECT_CREDIT = 2

# Stock Entry types that represent a net value add / remove / transform.
# Any type NOT in this set is silently skipped (transfers, subcontractor, etc.)
STOCK_ENTRY_PUSH_TYPES = frozenset({
    "Material Issue",
    "Material Receipt",
    "Manufacture",
    "Material Consumption for Manufacture",
    "Repack",
    "Disassemble",
})


# ---------------------------------------------------------------------------
# Custom field plumbing — three tracking fields on each supported doctype
# ---------------------------------------------------------------------------

_TRACKING_FIELDS = [
    (
        "custom_sage_order_id",
        "Sage Order ID",
        "Internal Sage ID assigned on successful push. Empty until first sync.",
    ),
    (
        "custom_sage_document_number",
        "Sage Document Number",
        "Human-readable document number Sage assigned (e.g. JE-2026-00123).",
    ),
    (
        "custom_sage_sync_status",
        "Sage Sync Status",
        "Last push result: Synced = landed in Sage; Failed = check error log.",
    ),
]


def _ensure_tracking_fields(doctype):
    """Idempotently add the three sync-tracking Custom Fields to *doctype*.

    Called from the worker on every run so a freshly-installed site picks them
    up automatically on the first push. Works for both 'Stock Entry' and
    'Stock Reconciliation'. Exits immediately if all three already exist.
    Same pattern as _ensure_journal_entry_tracking_fields in journal_entry.py.
    """
    for fieldname, label, description in _TRACKING_FIELDS:
        if frappe.db.exists(
            "Custom Field",
            {"dt": doctype, "fieldname": fieldname},
        ):
            continue
        try:
            frappe.get_doc(
                {
                    "doctype": "Custom Field",
                    "dt": doctype,
                    "fieldname": fieldname,
                    "label": label,
                    "fieldtype": "Data",
                    "read_only": 1,
                    "description": description,
                }
            ).insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(
                title=(
                    f"Sage Sync: could not create {fieldname} on {doctype}"
                )[:140],
                message=str(e),
            )


# ---------------------------------------------------------------------------
# Doc event wrappers — filter, then enqueue and return immediately
# ---------------------------------------------------------------------------

def post_stock_entry(doc, method):
    """doc_events['Stock Entry']['on_submit'] handler.

    Filters to push-eligible entry types only, then enqueues the shared worker
    so the submit transaction is not held open while Pharoh responds.
    """
    if not is_sync_enabled("push_stock_adjustment_on_submit"):
        return

    entry_type = doc.stock_entry_type or getattr(doc, "purpose", "") or ""
    if entry_type not in STOCK_ENTRY_PUSH_TYPES:
        # Material Transfer, Material Transfer for Manufacture,
        # Send to Subcontractor, etc. — no net value change, skip silently.
        return

    frappe.enqueue(
        "erpnext_sbca.API.stock_adjustment._post_stock_adjustment_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doctype="Stock Entry",
        doc_name=doc.name,
    )


def post_stock_reconciliation(doc, method):
    """doc_events['Stock Reconciliation']['on_submit'] handler.

    A Stock Reconciliation normally represents a net value correction that
    originated in ERPNext — its value change is pushed to Sage.

    EXCEPTION: an "Opening Stock" reconciliation is the one-time baseline
    created by the Stock tab's Import Stock Levels feature. Those quantities
    and values came FROM Sage in the first place — pushing them back would
    double-count. "Opening Stock" is a starting point, never an adjustment
    that originated in ERPNext, so it is always skipped here.

    Enqueues the shared worker so submit doesn't block on Pharoh.
    """
    if not is_sync_enabled("push_stock_adjustment_on_submit"):
        return

    if (doc.get("purpose") or "") == "Opening Stock":
        # Sage-originated opening baseline — never push back to Sage.
        return

    frappe.enqueue(
        "erpnext_sbca.API.stock_adjustment._post_stock_adjustment_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doctype="Stock Reconciliation",
        doc_name=doc.name,
    )


# ---------------------------------------------------------------------------
# Worker — builds payload and POSTs to Pharoh
# ---------------------------------------------------------------------------

def _post_stock_adjustment_worker(doctype, doc_name):
    """Build the GL-derived payload and POST to Pharoh for the document's company.

    Reads ERPNext GL Entry records for the submitted document and converts them
    to Sage journal lines. Matches the document's company to the correct Company
    Sage Integration row — avoids the multi-company cross-posting bug present in
    the POS invoice handler.

    Error handling mirrors journal_entry.py:
    - HTTP errors are logged with the full payload (truncated to 2000 chars).
    - Per-company errors set custom_sage_sync_status = "Failed" on the doc.
    - frappe.throw is called so the queue job shows as failed (visible in
      Scheduled Job Logs).
    """
    _ensure_tracking_fields(doctype)
    doc = frappe.get_doc(doctype, doc_name)
    payload = {}

    try:
        # Already-synced guard — don't double-post on worker retries.
        if doc.get("custom_sage_order_id"):
            return

        company = doc.company

        # Resolve the Company Sage Integration row for this document's company.
        # Stock documents are single-company — we push to exactly one tenant.
        settings = frappe.get_doc("Erpnext Sbca Settings")
        integration_name = frappe.db.get_value(
            "Company Sage Integration",
            {"parent": settings.name, "company": company},
            "name",
        )
        if not integration_name:
            frappe.log_error(
                title=(
                    f"Sage Stock Adjustment: no integration for '{company}'"
                )[:140],
                message=(
                    f"No Company Sage Integration row found for company "
                    f"'{company}'. Document: {doctype} {doc_name}.\n"
                    f"Add the integration row in Erpnext Sbca Settings → Connection."
                ),
            )
            return

        integration = frappe.get_doc("Company Sage Integration", integration_name)
        apikey = integration.get_password("api_key")
        if not apikey:
            frappe.throw(
                f"Sage credentials (API key) missing for '{company}'. "
                f"Fill in the Company Sage Integration row in Settings → Connection."
            )

        lines = _build_lines_from_gl(doctype, doc_name, company)
        if not lines:
            # Zero-value adjustment, or warehouses not linked to stock accounts.
            frappe.log_error(
                title=(
                    f"Sage Stock Adjustment: no GL entries — {doc_name}"
                )[:140],
                message=(
                    f"No GL entries found for {doctype} '{doc_name}' / "
                    f"company '{company}'. Nothing was pushed to Sage.\n"
                    f"If this is unexpected: check that the document submitted "
                    f"successfully and that its warehouses are linked to stock "
                    f"accounts in the Chart of Accounts."
                ),
            )
            return

        # Build a human-readable description for the Sage journal record.
        if doctype == "Stock Entry":
            entry_type = (
                doc.stock_entry_type or getattr(doc, "purpose", "") or "Stock Entry"
            )
            description = f"{entry_type} — {doc_name}"
        else:
            entry_type = "Stock Reconciliation"
            description = f"Stock Reconciliation — {doc_name}"

        payload = {
            "credentials": {
                "loginName": integration.username,
                "loginPwd": integration.get_password("password"),
                "useOAuth": bool(integration.use_oauth),
                "sessionToken": integration.get_password("session_id"),
                "provider": integration.get_password("provider"),
            },
            "stockAdjustment": {
                "date": frappe.utils.formatdate(doc.posting_date, "yyyy-MM-dd"),
                "reference": doc_name,
                "entryType": entry_type,
                "description": description,
                "memo": description,
                "taxPeriodId": None,
                "analysisCategoryId1": None,
                "analysisCategoryId2": None,
                "analysisCategoryId3": None,
                "trackingCode": "",
                "businessId": None,
                "payRunId": None,
                "lines": lines,
            },
        }

        endpoint_url = (
            f"{url}/api/StockAdjustmentSync/post-stock-adjustment-to-sage"
            f"?apikey={apikey}"
        )

        try:
            response = make_post_request(
                endpoint_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if response and response.get("success"):
                doc.db_set(
                    "custom_sage_order_id",
                    str(response.get("sageOrderId") or ""),
                )
                doc.db_set(
                    "custom_sage_document_number",
                    str(response.get("documentNumber") or ""),
                )
                try:
                    doc.db_set("custom_sage_sync_status", "Synced")
                except Exception:
                    pass
            else:
                error_msg = (
                    response.get("errorMessage") or str(response)
                    if response
                    else "Unknown error — Pharoh returned no response body."
                )
                frappe.throw(f"Sage API Error: {error_msg}")

        except Exception as http_err:
            err_str = str(http_err)
            sage_body = ""
            try:
                sage_body = http_err.response.text
            except Exception:
                sage_body = err_str
            frappe.log_error(
                title=(f"Sage Stock Adjustment HTTP Error — {doc_name}")[:140],
                message=(
                    f"HTTP Error: {err_str}\n"
                    f"Sage Response Body: {sage_body}\n"
                    f"Payload: {json.dumps(payload)[:2000]}"
                ),
            )
            raise

    except Exception as e:
        frappe.log_error(
            title=f"Sage Stock Adjustment Sync Error — {doc_name}"[:140],
            message=(
                f"DocType: {doctype}\n"
                f"Document: {doc_name}\n"
                f"Error: {str(e)}\n"
                f"Payload: {json.dumps(payload)[:2000] if payload else '<not built>'}"
            ),
        )
        try:
            doc.db_set("custom_sage_sync_status", "Failed")
        except Exception:
            pass
        frappe.throw(f"Sage Stock Adjustment Sync Failed: {str(e)}")


# ---------------------------------------------------------------------------
# GL Entry → Sage line conversion
# ---------------------------------------------------------------------------

def _build_lines_from_gl(voucher_type, voucher_no, company):
    """Read ERPNext GL Entries for a submitted document and convert to Sage lines.

    This is the core of the generic approach. ERPNext has already computed the
    exact DR/CR accounting for the stock movement. We translate account names to
    Sage account IDs and forward the result to Pharoh — no accounting logic of
    our own is needed.

    Args:
        voucher_type: ERPNext doctype string ("Stock Entry" or
                      "Stock Reconciliation").
        voucher_no:   The document name (e.g. "STE-2026-00042").
        company:      Company name — filters GL entries to this tenant only,
                      keeping multi-company setups clean.

    Returns:
        List of Sage line dicts ready to include in the payload. Empty list if
        no GL entries exist (zero-value adjustment, unlinked warehouses, etc.).

    Raises:
        frappe.ValidationError if an account has no Sage Account ID stamped, or
        if the stamped ID is non-numeric. These are actionable errors: either run
        the account sync or check the Chart of Accounts setup.
    """
    gl_entries = frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": voucher_type,
            "voucher_no": voucher_no,
            "company": company,
            "is_cancelled": 0,
        },
        fields=["account", "debit", "credit", "remarks"],
    )

    if not gl_entries:
        return []

    lines = []
    for entry in gl_entries:
        account_name = entry.account
        if not account_name:
            continue

        sage_account_id = frappe.db.get_value(
            "Account", account_name, "custom_sage_account_id"
        )
        if not sage_account_id:
            frappe.throw(
                f"Account '{account_name}' has no Sage Account ID. "
                f"Run the Sage account sync first, or verify that this account "
                f"is managed by Sage (custom_sage_managed = 1)."
            )

        try:
            account_id = int(sage_account_id)
        except (TypeError, ValueError):
            frappe.throw(
                f"Sage Account ID on '{account_name}' is not a valid integer: "
                f"{sage_account_id!r}. Check the account record."
            )

        debit = float(entry.debit or 0)
        credit = float(entry.credit or 0)

        # Skip zero-value GL entries (rounding artefacts, memo postings).
        if debit == 0 and credit == 0:
            continue

        if debit > 0:
            effect = SAGE_EFFECT_DEBIT
            amount = debit
        else:
            effect = SAGE_EFFECT_CREDIT
            amount = credit

        lines.append(
            {
                "effect": effect,
                "accountId": account_id,
                "debit": debit,
                "credit": credit,
                "exclusive": amount,
                "tax": 0,
                "total": amount,
                "taxTypeId": None,
                "description": entry.remarks or "",
            }
        )

    return lines
