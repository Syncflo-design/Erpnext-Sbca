# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""Push ERPNext Journal Entries to Sage on submit.

Triggered by `doc_events["Journal Entry"]["on_submit"]`. Gated by the
`push_journal_entry_on_submit` toggle on Erpnext Sbca Settings.

Pharoh endpoint: POST /api/JournalEntriesSync/post-journalentry-to-sage

ARCHITECTURE — multi-row journal mapping
----------------------------------------
ERPNext Journal Entries are multi-row (N accounts in the `accounts`
child table, debits and credits balanced overall). Sage's underlying
JournalEntry API takes a single (account, contra-account, debit, credit)
pair per call.

We send the full multi-row journal to Pharoh as ONE call. Pharoh is
responsible for decomposing it into Sage's single-pair shape and
posting the resulting rows under a single shared Reference so the
journal appears as one logical entry on the Sage side.

Decision locked 2026-05-13: one ERPNext journal -> one Sage journal,
matching the 1:1 model used for invoices.

PAYLOAD SHAPE (what Pharoh must accept)
---------------------------------------
{
  "credentials": {
    "loginName": "...", "loginPwd": "...",
    "useOAuth": false, "sessionToken": "...", "provider": "..."
  },
  "journalEntry": {
    "date": "yyyy-MM-dd",
    "reference": "<ERPNext JE name>",
    "description": "<user_remark>",
    "memo": "<user_remark>",
    "taxPeriodId": null,
    "analysisCategoryId1": null,
    "analysisCategoryId2": null,
    "analysisCategoryId3": null,
    "trackingCode": "",
    "businessId": null,
    "payRunId": null,
    "lines": [
      {
        "effect": 1,                    # 1=Debit, 2=Credit (Sage enum)
        "accountId": <Sage Account ID>,
        "debit": <decimal>,
        "credit": 0,
        "exclusive": <decimal>,
        "tax": 0,
        "total": <decimal>,
        "taxTypeId": null,
        "description": "<per-row user_remark>"
      },
      ...
    ]
  }
}

Null fields are explicit `null` (not 0) because Sage's API treats 0 as
an invalid foreign key reference. nullable integers per Sage docs:
TaxTypeId, TaxPeriodId, AnalysisCategoryId1/2/3, BusinessId, PayRunId.

PREREQUISITES
-------------
- ERPNext Account must carry `custom_sage_account_id` for each row's
  account. Populated by `account.get_accounts_from_sage` (added in the
  same change as this file). On a freshly-installed site, run the
  account sync at least once before submitting journals.
"""

import frappe
import json
from frappe.integrations.utils import make_post_request

url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled


SAGE_EFFECT_DEBIT = 1
SAGE_EFFECT_CREDIT = 2


# ---------------------------------------------------------------------------
# Custom field plumbing - Journal Entry tracking fields.
# ---------------------------------------------------------------------------

_TRACKING_FIELDS = [
    (
        "custom_sage_order_id",
        "Sage Order ID",
        "Internal Sage ID assigned to the pushed journal. Empty until "
        "the first successful push of this Journal Entry.",
    ),
    (
        "custom_sage_document_number",
        "Sage Document Number",
        "Human-readable document number Sage assigned to the pushed "
        "journal (e.g. JE-2026-00123).",
    ),
    (
        "custom_sage_sync_status",
        "Sage Sync Status",
        "Last push result. Synced = successfully landed in Sage; "
        "Failed = push errored (check the error log).",
    ),
]


def _ensure_journal_entry_tracking_fields():
    """Idempotently add the three sync-tracking Custom Fields to the
    Journal Entry doctype.

    Called from the worker on every run. Exits immediately if all
    fields already exist. Created here (rather than as fixtures) so a
    fresh `bench install-app` site picks them up automatically on the
    first JE push.

    Same pattern as the other _ensure_* helpers across the app.
    """
    for fieldname, label, description in _TRACKING_FIELDS:
        if frappe.db.exists(
            "Custom Field",
            {"dt": "Journal Entry", "fieldname": fieldname},
        ):
            continue
        try:
            frappe.get_doc(
                {
                    "doctype": "Custom Field",
                    "dt": "Journal Entry",
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
                    f"Sage Sync: could not create {fieldname} on Journal Entry"
                )[:140],
                message=str(e),
            )


def post_journal_entry(doc, method):
    """Wrapper: enqueue the push so submit doesn't block on Pharoh."""
    if not is_sync_enabled("push_journal_entry_on_submit"):
        return
    frappe.enqueue(
        "erpnext_sbca.API.journal_entry._post_journal_entry_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doc_name=doc.name,
    )


def _post_journal_entry_worker(doc_name):
    """Build the multi-row payload and POST to Pharoh, per Company Sage
    Integration row. Errors per Company are caught so a single tenant
    failing doesn't block the rest.
    """
    _ensure_journal_entry_tracking_fields()
    doc = frappe.get_doc("Journal Entry", doc_name)
    payload = {}

    try:
        if doc.get("custom_sage_order_id"):
            # Already synced — avoid double-posting on re-runs.
            return

        settings = frappe.get_doc("Erpnext Sbca Settings")
        integrations = frappe.db.get_all(
            "Company Sage Integration",
            filters={"parent": settings.name},
            fields=["name"],
        )

        if not doc.accounts:
            frappe.throw("Journal Entry has no rows in the Accounts table.")

        for integration_ref in integrations:
            try:
                integration = frappe.get_doc(
                    "Company Sage Integration", integration_ref.name
                )
                apikey = integration.get_password("api_key")
                if not apikey:
                    frappe.throw(
                        f"Sage credentials missing for {integration.company}."
                    )

                lines = _build_lines(doc, integration.company)
                if not lines:
                    # Nothing balanced to send (e.g. zero-value rows only).
                    continue

                payload = {
                    "credentials": {
                        "loginName": integration.username,
                        "loginPwd": integration.get_password("password"),
                        "useOAuth": bool(integration.use_oauth),
                        "sessionToken": integration.get_password("session_id"),
                        "provider": integration.get_password("provider"),
                    },
                    "journalEntry": {
                        "date": frappe.utils.formatdate(
                            doc.posting_date, "yyyy-MM-dd"
                        ),
                        "reference": doc.name or "",
                        "description": doc.user_remark or "",
                        "memo": doc.user_remark or "",
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
                    f"{url}/api/JournalEntriesSync/post-journalentry-to-sage"
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
                            response.get("errorMessage")
                            or str(response)
                            if response
                            else "Unknown"
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
                        title=(
                            f"Sage Journal Entry HTTP Error - {doc.name}"
                        )[:140],
                        message=(
                            f"HTTP Error: {err_str}\n"
                            f"Sage Response Body: {sage_body}\n"
                            f"Payload: {json.dumps(payload)[:2000]}"
                        ),
                    )
                    raise

            except Exception as per_company_e:
                frappe.log_error(
                    title=(
                        f"Sage JE Sync Failed - {doc.name} "
                        f"({integration.company if 'integration' in locals() else 'unknown'})"
                    )[:140],
                    message=str(per_company_e),
                )

    except Exception as e:
        frappe.log_error(
            title="Sage Journal Entry Sync Error"[:140],
            message=(
                f"Journal Entry: {doc.name}\n"
                f"Error: {str(e)}\n"
                f"Payload: {json.dumps(payload)[:2000] if payload else '<not built>'}"
            ),
        )
        try:
            doc.db_set("custom_sage_sync_status", "Failed")
        except Exception:
            pass
        frappe.throw(f"Sage Sync Failed: {str(e)}")


def _build_lines(doc, company_name):
    """Convert ERPNext Journal Entry rows into the Sage line shape.

    For each row:
      - Resolve the account's Sage ID via custom_sage_account_id.
      - Decide effect (1 for debit-side, 2 for credit-side) based on
        which of debit / credit is non-zero.
      - Use company-currency amounts (`debit` / `credit`) — Sage is the
        financial ledger, posting is in company currency.

    Throws cleanly when an account can't be resolved so the user knows
    exactly which row needs attention.
    """
    lines = []
    for row in doc.accounts:
        account_name = row.account
        if not account_name:
            continue

        sage_account_id = frappe.db.get_value(
            "Account", account_name, "custom_sage_account_id"
        )
        if not sage_account_id:
            frappe.throw(
                f"Account '{account_name}' has no Sage Account ID. "
                f"Run the Sage account sync first, or check that this "
                f"account is one Sage manages."
            )

        try:
            account_id = int(sage_account_id)
        except (TypeError, ValueError):
            frappe.throw(
                f"Sage Account ID on '{account_name}' is not numeric: "
                f"{sage_account_id!r}"
            )

        debit = float(row.debit or 0)
        credit = float(row.credit or 0)

        # Skip zero rows (e.g. memo-only entries) — nothing to post.
        if debit == 0 and credit == 0:
            continue

        if debit > 0 and credit > 0:
            frappe.throw(
                f"Journal Entry row against '{account_name}' has both a "
                f"debit and a credit. Sage expects one direction per row."
            )

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
                "description": row.user_remark or "",
            }
        )
    return lines
