# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""Pull Sales Persons from Sage into ERPNext.

Runs as a scheduler tick, gated by the `sync_sales_persons` toggle on
Erpnext Sbca Settings. Walks every Company Sage Integration row and
POSTs to /api/SalesReps/get-salesperson-for-erpnext.

Match key: `custom_sage_rep_id` (the Sage `id`) first; fallback to
`sales_person_name` for first-time records that have no Sage ID yet.
On every successful upsert, `custom_sage_rep_id` is (re)written from
the payload's `id` so the downstream sales_invoice push has what it
needs.

Customer pull depends on Sales Persons existing — `customer.py`'s
sales_team handling looks them up by name. Order matters in hooks.py:
sales_person before customer.
"""

import frappe
from frappe.integrations.utils import make_post_request

url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled, safe_strip, chunks


# ---------------------------------------------------------------------------
# Custom field plumbing
# ---------------------------------------------------------------------------

def _ensure_sage_rep_id_field():
    """Idempotently add `custom_sage_rep_id` to Sales Person.

    sales_invoice.py reads this field at push time (and falls back to a
    hardcoded 740886 if it's missing). Once this pull runs, every Sales
    Person it touches will carry the real Sage ID and the fallback
    becomes irrelevant.
    """
    if frappe.db.exists(
        "Custom Field",
        {"dt": "Sales Person", "fieldname": "custom_sage_rep_id"},
    ):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Sales Person",
                "fieldname": "custom_sage_rep_id",
                "label": "Sage Rep ID",
                "fieldtype": "Data",
                "read_only": 1,
                "description": (
                    "Set by the Sage Sales Person sync. Used by the sales/POS "
                    "invoice push when building Credit Notes."
                ),
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            title="Sage Sync: could not create custom_sage_rep_id field",
            message=str(e),
        )


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------

def get_sales_persons_from_sage():
    """Pull Sales Persons from Sage into ERPNext.

    Gated by `sync_sales_persons` toggle. Walks Company Sage Integration
    rows and upserts Sales Person records by Sage rep ID, falling back
    to name for first-time records.
    """
    if not is_sync_enabled("sync_sales_persons"):
        return

    _ensure_sage_rep_id_field()

    settings = frappe.get_doc("Erpnext Sbca Settings")
    company_settings = frappe.db.get_all(
        "Company Sage Integration",
        filters={"parent": settings.name},
        fields=["name"],
    )

    for company in company_settings:
        try:
            company = frappe.get_doc("Company Sage Integration", company.name)
            apikey = company.get_password("api_key")
            payload = {
                "loginName": company.username,
                "loginPwd": company.get_password("password"),
                "useOAuth": bool(company.use_oauth),
                "sessionToken": company.get_password("session_id"),
                "provider": company.get_password("provider"),
            }
            endpoint_url = f"{url}/api/SalesReps/get-salesperson-for-erpnext?apikey={apikey}"

            reps = make_post_request(endpoint_url, json=payload)

            if not isinstance(reps, list):
                frappe.log_error(
                    title=f"Sage Sales Person Sync: unexpected response shape for {company.company}"[:140],
                    message=f"Expected JSON array, got {type(reps).__name__}: {reps}",
                )
                continue

            created = []
            updated = []
            skipped = []

            batch_size = 50
            for batch in chunks(reps, batch_size):
                for rep_data in batch:
                    sage_id = safe_strip(rep_data.get("id"))
                    rep_name = safe_strip(rep_data.get("sales_person_name"))
                    if not sage_id and not rep_name:
                        skipped.append(None)
                        continue

                    try:
                        _upsert_sales_person(
                            sage_id=sage_id,
                            rep_data=rep_data,
                            created=created,
                            updated=updated,
                            skipped=skipped,
                        )
                    except Exception as e:
                        frappe.log_error(
                            title=f"Error processing Sales Person {rep_name or sage_id}"[:140],
                            message=str(e),
                        )
                        skipped.append(rep_name or sage_id)

                frappe.db.commit()

            summary = (
                f"Company: {company.company} | "
                f"Created: {len(created)}, "
                f"Updated: {len(updated)}, "
                f"Skipped: {len(skipped)}"
            )
            frappe.log_error(
                title=f"Sage Sales Person Sync Summary for {company.company}"[:140],
                message=summary,
            )

        except Exception as e:
            frappe.log_error(
                title=f"Sage Sales Person Sync Fatal Error for {company.company}"[:140],
                message=str(e),
            )


def _upsert_sales_person(sage_id, rep_data, created, updated, skipped):
    """Insert-or-update a Sales Person record.

    Match order:
      1. custom_sage_rep_id = sage_id (strongest match)
      2. sales_person_name = stripped name (for records that pre-date the sync)
    """
    rep_name = safe_strip(rep_data.get("sales_person_name"))
    email = safe_strip(rep_data.get("email_id")) or ""
    mobile = safe_strip(rep_data.get("mobile_no")) or ""
    enabled = 1 if rep_data.get("active") else 0

    existing_name = None
    if sage_id:
        existing_name = frappe.db.get_value(
            "Sales Person", {"custom_sage_rep_id": sage_id}, "name"
        )
    if not existing_name and rep_name:
        existing_name = frappe.db.get_value(
            "Sales Person", {"sales_person_name": rep_name}, "name"
        )

    if existing_name:
        doc = frappe.get_doc("Sales Person", existing_name)
        if rep_name:
            doc.sales_person_name = rep_name
        doc.enabled = enabled
        # email_id / mobile_no aren't standard Sales Person fields in every
        # ERPNext version — set defensively only if the field exists on the
        # doctype.
        if hasattr(doc, "email_id"):
            doc.email_id = email
        if hasattr(doc, "mobile_no"):
            doc.mobile_no = mobile
        doc.custom_sage_rep_id = sage_id or doc.get("custom_sage_rep_id")
        doc.save(ignore_permissions=True)
        updated.append(rep_name or sage_id)
    else:
        if not rep_name:
            skipped.append(sage_id)
            return
        doc_dict = {
            "doctype": "Sales Person",
            "sales_person_name": rep_name,
            "enabled": enabled,
            "is_group": 0,
            "custom_sage_rep_id": sage_id or "",
        }
        new_doc = frappe.get_doc(doc_dict)
        # Set optional fields only if the doctype has them on this site.
        if hasattr(new_doc, "email_id"):
            new_doc.email_id = email
        if hasattr(new_doc, "mobile_no"):
            new_doc.mobile_no = mobile
        new_doc.insert(ignore_permissions=True)
        created.append(rep_name)
