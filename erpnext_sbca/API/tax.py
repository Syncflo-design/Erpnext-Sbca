# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""Tax handling for the Sage <-> ERPNext bridge.

This module owns three things:

1. The custom field plumbing — adds `custom_sage_tax_map` (a Table field
   pointing at the `Item Tax Template Sage Map` child doctype) to the
   standard ERPNext `Item Tax Template`. Installed idempotently via
   `_ensure_sage_tax_map_field()`, mirroring the pattern in account.py's
   `_ensure_sage_managed_field()`.

2. The pull — `get_taxes_from_sage()` walks every `Company Sage
   Integration` row, calls `/api/SalesTaxSync/get-sales-taxes-for-erpnext`
   with that tenant's credentials, and upserts one `Sage Tax` record per
   (template, child tax row) pair returned. Records that stop being
   returned get marked `disabled = 1` rather than deleted, so existing
   Item Tax Template mappings still resolve.

3. The push-time helpers — `resolve_sage_tax()` finds the correct Sage
   Tax record for a given (item, company, direction) triple via the
   mapping table. `build_price_pair()` produces the (excl, incl) price
   pair using the real per-tenant tax rate and the auto-detect rule for
   inclusive- vs exclusive-driven items.

These helpers replace every hardcoded `tax_id` read and every hardcoded
`* 1.15` multiplication in items.py, sales_invoice.py,
purchase_invoice.py, and pos_invoice.py.
"""

import frappe
from frappe.integrations.utils import make_post_request
from frappe.utils import now_datetime


# ---------------------------------------------------------------------------
# 1. Custom field plumbing
# ---------------------------------------------------------------------------

def _ensure_sage_tax_map_field():
    """Idempotently add `custom_sage_tax_map` to Item Tax Template.

    The field is a Table pointing at the `Item Tax Template Sage Map`
    child doctype, where each row pairs the template with the Sage Tax
    records to use per Company.

    Safe to call on every install / migrate — if the field already
    exists, the function returns immediately. Errors are logged but
    don't crash the install (matches account.py's pattern).
    """
    if frappe.db.exists(
        "Custom Field",
        {"dt": "Item Tax Template", "fieldname": "custom_sage_tax_map"},
    ):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Item Tax Template",
                "fieldname": "custom_sage_tax_map",
                "label": "Sage Tax Mappings",
                "fieldtype": "Table",
                "options": "Item Tax Template Sage Map",
                "insert_after": "taxes",
                "description": (
                    "Pairs this Item Tax Template with Sage Tax records "
                    "per Company. Populated manually after pulling the "
                    "Sage tax catalogue via the Pull Taxes button on "
                    "Erpnext Sbca Settings."
                ),
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            title="Sage Sync: could not create custom_sage_tax_map field",
            message=str(e),
        )


# ---------------------------------------------------------------------------
# 2. Pull
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_taxes_from_sage():
    """Pull the Sage tax catalogue for every Company Sage Integration row.

    Whitelisted so the 'Pull Taxes from Sage' button on Erpnext Sbca
    Settings can trigger it. The button is the only intended entry
    point — there is deliberately no scheduled task for this (taxes
    change rarely).

    For each `Company Sage Integration` row:
      - POSTs to /api/SalesTaxSync/get-sales-taxes-for-erpnext with
        the tenant's credentials envelope.
      - Flattens the templates-with-children response: one Sage Tax
        record per child tax row.
      - Upserts by (company, sage_idx). Re-enables previously-disabled
        records that reappear. Sets `last_seen_at = now` on every
        record that came back.
      - After the upsert pass, any Sage Tax for this Company whose
        sage_idx did NOT appear in the response gets `disabled = 1`.
        Rows are never deleted, so existing Item Tax Template mappings
        keep resolving (with a warning surfaced at push time).

    Returns a list of per-Company summaries:
        [
          {"company": "Syncflo (Pty) Ltd",
           "created": 11, "updated": 0, "disabled": 0,
           "errors": []},
          ...
        ]
    so the client-side button can show a toast.
    """
    # Belt-and-braces: make sure the Item Tax Template custom field exists
    # before the user starts mapping. Idempotent; matches the pattern
    # account.py uses for custom_sage_managed.
    _ensure_sage_tax_map_field()

    settings = frappe.get_doc("Erpnext Sbca Settings")
    base_url = (settings.url or "").rstrip("/")
    if not base_url:
        frappe.throw("Erpnext Sbca Settings: URL is not set.")

    integration_rows = frappe.db.get_all(
        "Company Sage Integration",
        filters={"parent": settings.name},
        fields=["name"],
    )
    if not integration_rows:
        return []

    summaries = []
    for row_ref in integration_rows:
        summary = _pull_taxes_for_company(base_url, row_ref.name)
        summaries.append(summary)
    return summaries


def _pull_taxes_for_company(base_url, integration_row_name):
    """Pull the tax catalogue for one Company Sage Integration row.

    Returns a summary dict. Catches all per-record exceptions so one
    bad payload doesn't abort the rest of the pull.
    """
    integration = frappe.get_doc("Company Sage Integration", integration_row_name)
    company_name = integration.company
    summary = {
        "company": company_name or f"<unlinked row {integration_row_name}>",
        "created": 0,
        "updated": 0,
        "disabled": 0,
        "errors": [],
    }

    if not company_name:
        summary["errors"].append(
            f"Integration row {integration_row_name} has no Company set — skipped."
        )
        return summary

    apikey = integration.get_password("api_key")
    if not apikey:
        summary["errors"].append("Missing API key — skipped.")
        return summary

    payload = {
        "loginName": integration.username,
        "loginPwd": integration.get_password("password"),
        "useOAuth": bool(integration.use_oauth),
        "sessionToken": integration.get_password("session_id"),
        "provider": integration.get_password("provider"),
    }
    endpoint_url = (
        f"{base_url}/api/SalesTaxSync/get-sales-taxes-for-erpnext?apikey={apikey}"
    )

    try:
        response = make_post_request(
            endpoint_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
    except Exception as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            body = str(e)
        summary["errors"].append(f"HTTP error from Sage: {body[:500]}")
        frappe.log_error(
            title=f"Sage Tax Pull HTTP Error for {company_name}"[:140],
            message=f"Error: {e}\nResponse body: {body}",
        )
        return summary

    if not isinstance(response, list):
        summary["errors"].append(
            f"Unexpected response shape (expected JSON array, got "
            f"{type(response).__name__})."
        )
        return summary

    seen_sage_idx = set()
    now = now_datetime()

    for template in response:
        if not isinstance(template, dict):
            continue
        template_label = (template.get("name") or "").strip()
        for child in template.get("taxes") or []:
            if not isinstance(child, dict):
                continue
            sage_idx_raw = child.get("sage_idx")
            if sage_idx_raw is None or sage_idx_raw == "":
                continue
            sage_idx = str(sage_idx_raw).strip()
            rate = _safe_float(child.get("rate"))
            description = (child.get("description") or "").strip()

            try:
                _upsert_sage_tax(
                    company_name=company_name,
                    sage_idx=sage_idx,
                    description=description,
                    rate=rate,
                    template_label=template_label,
                    last_seen_at=now,
                    summary=summary,
                )
                seen_sage_idx.add(sage_idx)
            except Exception as e:
                summary["errors"].append(
                    f"Upsert failed for sage_idx={sage_idx}: {e}"
                )
                frappe.log_error(
                    title=(
                        f"Sage Tax Pull Upsert Error "
                        f"{company_name}/{sage_idx}"
                    )[:140],
                    message=str(e),
                )

    # Stale-record sweep — disable any Sage Tax row for this Company
    # whose sage_idx did NOT come back in this pull.
    existing = frappe.db.get_all(
        "Sage Tax",
        filters={"company": company_name, "disabled": 0},
        fields=["name", "sage_idx"],
    )
    for row in existing:
        if row.sage_idx not in seen_sage_idx:
            frappe.db.set_value("Sage Tax", row.name, "disabled", 1)
            summary["disabled"] += 1

    frappe.db.commit()
    return summary


def _upsert_sage_tax(
    company_name,
    sage_idx,
    description,
    rate,
    template_label,
    last_seen_at,
    summary,
):
    """Insert-or-update one Sage Tax row, keyed on (company, sage_idx)."""
    existing_name = frappe.db.get_value(
        "Sage Tax",
        {"company": company_name, "sage_idx": sage_idx},
        "name",
    )
    if existing_name:
        doc = frappe.get_doc("Sage Tax", existing_name)
        doc.description = description
        doc.rate = rate
        doc.template_label = template_label
        # Re-enable if a previously-stale record reappears.
        doc.disabled = 0
        doc.last_seen_at = last_seen_at
        doc.save(ignore_permissions=True)
        summary["updated"] += 1
    else:
        frappe.get_doc(
            {
                "doctype": "Sage Tax",
                "company": company_name,
                "sage_idx": sage_idx,
                "description": description,
                "rate": rate,
                "template_label": template_label,
                "disabled": 0,
                "last_seen_at": last_seen_at,
            }
        ).insert(ignore_permissions=True)
        summary["created"] += 1


def _safe_float(value):
    """Coerce a Sage payload value to float. None / blank / garbage -> 0.0.

    Sage's rate field is decimal-fraction (0.15 = 15%); we pass it
    through unchanged. Special values (e.g. 1.0 for the import-VAT
    categories that effectively mean 100%) are honoured as-is.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 3. Push-time helpers
# ---------------------------------------------------------------------------

def resolve_sage_tax(item_doc, company_name, direction):
    """Return the Sage Tax doc to use for this (item, company, direction).

    direction: "sales" or "purchases".

    Resolution chain:
        item -> first Item Tax Template on item.taxes
             -> mapping row in template.custom_sage_tax_map where
                company == company_name
             -> sales_sage_tax or purchase_sage_tax (per direction)
             -> Sage Tax doc

    Raises with a precise message at the first gap so the user knows
    exactly what to fix. Disabled Sage Tax records still resolve, but
    a warning is logged so the misalignment is visible.
    """
    if direction not in ("sales", "purchases"):
        frappe.throw(
            f"resolve_sage_tax: direction must be 'sales' or 'purchases', "
            f"got {direction!r}."
        )

    item_label = getattr(item_doc, "item_code", None) or getattr(
        item_doc, "name", "<unknown item>"
    )

    if not getattr(item_doc, "taxes", None):
        frappe.throw(
            f"Item {item_label} has no Item Tax Template assigned. "
            f"Add a row under Item Taxes."
        )

    template_name = item_doc.taxes[0].item_tax_template
    if not template_name:
        frappe.throw(
            f"Item {item_label}'s first Item Taxes row has no Item Tax "
            f"Template set."
        )

    template = frappe.get_doc("Item Tax Template", template_name)
    mappings = [
        m
        for m in (template.get("custom_sage_tax_map") or [])
        if m.company == company_name
    ]
    if not mappings:
        frappe.throw(
            f"Item Tax Template '{template_name}' has no Sage tax mapping "
            f"for company '{company_name}'. Add a row in Sage Tax Mappings "
            f"on that template."
        )

    mapping = mappings[0]
    target_field = "sales_sage_tax" if direction == "sales" else "purchase_sage_tax"
    sage_tax_name = mapping.get(target_field)
    if not sage_tax_name:
        frappe.throw(
            f"Item Tax Template '{template_name}' has no {direction} Sage "
            f"tax set for company '{company_name}'. Set "
            f"'{target_field}' on the mapping row."
        )

    sage_tax = frappe.get_doc("Sage Tax", sage_tax_name)

    if sage_tax.get("disabled"):
        # Don't block the push — historical mappings stay functional —
        # but log so the misalignment shows up in the error log.
        frappe.log_error(
            title=(
                f"Sage Tax push warning: disabled record in use - "
                f"{sage_tax_name}"
            )[:140],
            message=(
                f"Item Tax Template '{template_name}' is mapped to "
                f"Sage Tax '{sage_tax_name}' for company "
                f"'{company_name}', but that record is marked disabled "
                f"(last Sage pull did not return it). The push will "
                f"still send the stored sage_idx and rate. Re-pull the "
                f"Sage tax catalogue, then update this mapping."
            ),
        )

    return sage_tax


def build_price_pair(item_doc, rate):
    """Return (excl, incl) for an item, honouring the pricing direction.

    Auto-detect rule:
      - If doc.custom_retail_price_incl_vat > 0  -> inclusive-driven.
        The retail incl. price is the source of truth, kept at 2dp;
        the excl. is computed backwards and kept at 4dp so the
        round-trip excl * (1 + rate) -> incl is exact at 2dp.
      - Else                                     -> exclusive-driven.
        doc.standard_rate is the source of truth, kept at 2dp;
        the incl. is computed forwards and rounded to 2dp.

    `rate` is a decimal fraction (Sage convention): 0.15 == 15%.

    Returns (excl, incl) as floats, ready to drop into the push payload.
    """
    multiplier = 1 + float(rate or 0)
    if multiplier <= 0:
        # Pathological input — never expected from real Sage data, but
        # we don't want a silent ZeroDivisionError further down.
        frappe.throw(
            f"build_price_pair: invalid rate {rate!r} (would zero or "
            f"negate the multiplier)."
        )

    incl_source = float(item_doc.get("custom_retail_price_incl_vat") or 0)
    if incl_source > 0:
        incl = round(incl_source, 2)
        excl = round(incl / multiplier, 4)
    else:
        excl_source = float(item_doc.get("standard_rate") or 0)
        excl = round(excl_source, 2)
        incl = round(excl * multiplier, 2)

    return excl, incl


# ---------------------------------------------------------------------------
# 4. Status read for the Settings page
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_tax_status():
    """Per-Company snapshot of the Sage Tax catalogue.

    Returns one row per Company Sage Integration record:
        {
          "company": <Company name>,
          "active":   N,   # disabled = 0
          "disabled": N,   # disabled = 1 (stale, mappings still resolve)
          "last_seen_at": <datetime or None>,
        }

    Used by the Settings page Taxes tab to render the catalogue status
    banner. Whitelisted so the client script can call it freely.
    """
    integrations = frappe.db.get_all(
        "Company Sage Integration",
        fields=["company"],
    )
    out = []
    seen = set()
    for row in integrations:
        company = row.company
        if not company or company in seen:
            continue
        seen.add(company)
        out.append(_company_tax_status(company))
    return out


def _company_tax_status(company_name):
    active = frappe.db.count(
        "Sage Tax", {"company": company_name, "disabled": 0}
    )
    disabled = frappe.db.count(
        "Sage Tax", {"company": company_name, "disabled": 1}
    )
    last_row = frappe.db.get_all(
        "Sage Tax",
        filters={"company": company_name},
        fields=["last_seen_at"],
        order_by="last_seen_at desc",
        limit=1,
    )
    last_seen = last_row[0].last_seen_at if last_row else None
    return {
        "company": company_name,
        "active": active,
        "disabled": disabled,
        "last_seen_at": last_seen,
    }

