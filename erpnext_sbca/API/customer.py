# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""Pull Customers from Sage into ERPNext.

Runs as a scheduler tick, gated by `sync_customers` toggle on Erpnext
Sbca Settings. POSTs to /api/CustomersSync/get-customers-for-erpnext
for each Company Sage Integration row.

Match key: `custom_sage_customer_id` (the Sage `name` field) first;
fallback to `customer_name` for first-time records. Always populates
`custom_sage_customer_id` on every successful upsert so the existing
sales_invoice / sales_order / pos_invoice push paths have what they
need.

V1 scope (intentional skips):
  - credit_limits     - flat in Sage, per-company in ERPNext; revisit
                        when we know which Company to attach to.
  - addresses         - 5-line Sage format doesn't cleanly map to
                        ERPNext Address. Wait for explicit request.
  - default_tax_typeId - stored verbatim in `custom_sage_default_tax_
                        type_id` for later; not mapped to tax_category.
  - tax_type nested object - redundant with default_tax_typeId.

default_price_list_id is resolved via custom_sage_price_list_id on
the Price List (populated by item_details.get_price_list_from_sage).
Missing matches are silent - typical before the first Price List pull.

sales_team is honoured: each row references a Sales Person by name
(stripped). If the named Sales Person doesn't exist in ERPNext, that
row is skipped (logged) but the customer upsert still succeeds. Run
the Sales Person pull first so this resolves cleanly.

Per-tenant scoping for V1: all Sage customers across all tenants land
as a single global set in ERPNext, last-tenant-wins on shared records.
True per-tenant isolation is a separate task.
"""

import frappe
from frappe.integrations.utils import make_post_request

url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import (
    is_sync_enabled,
    safe_strip,
    chunks,
    ensure_party_group,
    fetch_all_pages,
)


# ---------------------------------------------------------------------------
# Default-group resolvers - pick a leaf (non-group) row at runtime so we
# never try to assign a Customer to a Customer Group / Territory that is
# itself a group-tree node (ERPNext rejects with "Cannot select a Group
# type ...").
# ---------------------------------------------------------------------------

def _default_customer_group():
    """First enabled leaf Customer Group on this site.

    ERPNext ships defaults like 'Commercial', 'Individual' etc. as leaves
    under the 'All Customer Groups' root. Picking the first one keeps the
    pull adaptive - if the site renames its leaves we still resolve a
    valid value. Falls back to 'Commercial' if nothing else is available.
    """
    return frappe.db.get_value(
        "Customer Group",
        {"is_group": 0},
        "name",
        order_by="creation asc",
    ) or "Commercial"


def _default_territory():
    """First enabled leaf Territory on this site.

    Same defensive pattern as _default_customer_group(). Falls back to
    the ERPNext-standard 'Rest Of The World' if no leaves exist.
    """
    return frappe.db.get_value(
        "Territory",
        {"is_group": 0},
        "name",
        order_by="creation asc",
    ) or "Rest Of The World"


def get_customer_categories_from_sage():
    """Mirror Sage's Customer Categories into ERPNext as leaf Customer Groups.

    Gated by the `sync_customer_categories` toggle. Runs ahead of
    get_customers_from_sage in the scheduler so the groups exist before the
    customer pull assigns parties into them (the customer pull also creates
    them lazily via ensure_party_group, so the ordering is a nicety, not a
    hard requirement).

    Pharoh endpoint: POST /api/CustomersSync/get-customer-categories-for-erpnext
    Response: a bare JSON array of {description, id, modified, created}. Only
    `description` (the category name) is used -- each becomes a leaf Customer
    Group under "All Customer Groups". The Sage `id` is intentionally not
    stored: customer records carry the category by name, so the name is the
    join key (renames are rare -- revisit id-tracking only if that changes).
    """
    if not is_sync_enabled("sync_customer_categories"):
        return

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
            endpoint_url = (
                f"{url}/api/CustomersSync/get-customer-categories-for-erpnext"
                f"?apikey={apikey}"
            )

            categories = make_post_request(endpoint_url, json=payload)
            if not isinstance(categories, list):
                frappe.log_error(
                    title=(
                        f"Sage Customer Category Sync: unexpected response "
                        f"for {company.company}"
                    )[:140],
                    message=(
                        f"Expected a JSON array, got "
                        f"{type(categories).__name__}: {categories}"
                    ),
                )
                continue

            ensured = 0
            for cat in categories:
                if isinstance(cat, dict) and ensure_party_group(
                    "Customer Group", cat.get("description")
                ):
                    ensured += 1
            frappe.db.commit()
            frappe.logger("sbca").info(
                f"Sage Customer Category Sync {company.company}: "
                f"{len(categories)} categories returned, {ensured} groups ensured."
            )

        except Exception as e:
            frappe.log_error(
                title=(
                    f"Sage Customer Category Sync Fatal Error for "
                    f"{company.company}"
                )[:140],
                message=str(e),
            )



# ---------------------------------------------------------------------------
# Custom field plumbing
# ---------------------------------------------------------------------------

def _ensure_sage_customer_id_field():
    """Idempotently add `custom_sage_customer_id` to Customer.

    sales_invoice.py / sales_order.py / pos_invoice.py all read this
    field at push time and frappe.throw if missing. This field is the
    pivot point between the two systems for any customer-facing push.
    """
    if frappe.db.exists(
        "Custom Field",
        {"dt": "Customer", "fieldname": "custom_sage_customer_id"},
    ):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Customer",
                "fieldname": "custom_sage_customer_id",
                "label": "Sage Customer ID",
                "fieldtype": "Data",
                "read_only": 1,
                "description": (
                    "Set by the Sage Customer sync from Sage's `name` field. "
                    "Used by every sales-side push to identify the customer "
                    "on the Sage side."
                ),
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            title="Sage Sync: could not create custom_sage_customer_id field",
            message=str(e),
        )


def _ensure_sage_default_tax_type_id_field():
    """Idempotently add `custom_sage_default_tax_type_id` to Customer.

    Stored verbatim from Sage's `default_tax_typeId` so it's
    recoverable later if/when we map it into ERPNext's tax_category.
    """
    if frappe.db.exists(
        "Custom Field",
        {"dt": "Customer", "fieldname": "custom_sage_default_tax_type_id"},
    ):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Customer",
                "fieldname": "custom_sage_default_tax_type_id",
                "label": "Sage Default Tax Type ID",
                "fieldtype": "Data",
                "read_only": 1,
                "description": (
                    "Sage's `default_tax_typeId` for this customer. "
                    "Stored verbatim; not currently used in tax routing."
                ),
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            title="Sage Sync: could not create custom_sage_default_tax_type_id field",
            message=str(e),
        )


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------

def get_customers_from_sage():
    """Pull Customers from Sage into ERPNext.

    Gated by `sync_customers` toggle. Walks Company Sage Integration
    rows and upserts Customer records by Sage customer ID, falling
    back to customer_name for first-time matches.
    """
    if not is_sync_enabled("sync_customers"):
        return

    _ensure_sage_customer_id_field()
    _ensure_sage_default_tax_type_id_field()

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
            endpoint_url = f"{url}/api/CustomersSync/get-customers-for-erpnext?apikey={apikey}"

            # Pharoh paginates this endpoint — fetch_all_pages drives the
            # skipQty loop and returns the combined customer list.
            customers = fetch_all_pages(endpoint_url, payload)

            created = []
            updated = []
            skipped = []
            sales_team_skips = []  # named Sales Persons not found in ERPNext

            batch_size = 50
            for batch in chunks(customers, batch_size):
                for cust_data in batch:
                    sage_id = safe_strip(cust_data.get("name"))
                    cust_name = safe_strip(cust_data.get("customer_name"))
                    if not sage_id and not cust_name:
                        skipped.append(None)
                        continue

                    try:
                        _upsert_customer(
                            sage_id=sage_id,
                            cust_data=cust_data,
                            created=created,
                            updated=updated,
                            skipped=skipped,
                            sales_team_skips=sales_team_skips,
                        )
                    except Exception as e:
                        frappe.log_error(
                            title=f"Error processing Customer {cust_name or sage_id}"[:140],
                            message=str(e),
                        )
                        skipped.append(cust_name or sage_id)

                frappe.db.commit()

            summary_parts = [
                f"Company: {company.company}",
                f"Created: {len(created)}",
                f"Updated: {len(updated)}",
                f"Skipped: {len(skipped)}",
            ]
            if sales_team_skips:
                summary_parts.append(
                    f"Sales-team rows skipped (Sales Person not found): "
                    f"{len(sales_team_skips)}"
                )
            frappe.log_error(
                title=f"Sage Customer Sync Summary for {company.company}"[:140],
                message=" | ".join(summary_parts),
            )

        except Exception as e:
            frappe.log_error(
                title=f"Sage Customer Sync Fatal Error for {company.company}"[:140],
                message=str(e),
            )


def _upsert_customer(sage_id, cust_data, created, updated, skipped, sales_team_skips):
    """Insert-or-update a Customer record.

    Match order:
      1. custom_sage_customer_id = sage_id
      2. customer_name = stripped name (for pre-existing ERPNext customers)
    """
    cust_name = safe_strip(cust_data.get("customer_name"))

    existing_name = None
    if sage_id:
        existing_name = frappe.db.get_value(
            "Customer", {"custom_sage_customer_id": sage_id}, "name"
        )
    if not existing_name and cust_name:
        existing_name = frappe.db.get_value(
            "Customer", {"customer_name": cust_name}, "name"
        )

    # Resolved per-Customer values reused on insert + update.
    resolved = {
        "customer_name": cust_name,
        "customer_type": "Company",  # Sage has no native customer_type
        "customer_group": (
            ensure_party_group("Customer Group", cust_data.get("customer_group"))
            or _default_customer_group()
        ),
        "territory": _default_territory(),
        "email_id": safe_strip(cust_data.get("email_id")) or "",
        "mobile_no": safe_strip(cust_data.get("mobile_no")) or "",
        "language": safe_strip(cust_data.get("language")) or "en",
        "default_commission_rate": cust_data.get("default_commission_rate") or 0,
        "so_required": 1 if cust_data.get("so_required") else 0,
        "dn_required": 1 if cust_data.get("dn_required") else 0,
        "is_frozen": 1 if cust_data.get("is_frozen") else 0,
        "is_internal_customer": 1 if cust_data.get("is_internal_customer") else 0,
        "custom_sage_customer_id": sage_id or "",
        "custom_sage_default_tax_type_id": _to_str(
            cust_data.get("default_tax_typeId")
        ),
    }

    # Resolve default_price_list_id (Sage ID) -> ERPNext Price List name
    # via the custom_sage_price_list_id stamped by get_price_list_from_sage.
    # Only set when we can match; missing matches are silent (typical on
    # first sync before price lists have been pulled).
    # Wrapped in try/except in case the Custom Field hasn't been created
    # yet on this site (would raise unknown-column from the DB).
    sage_pl_id = cust_data.get("default_price_list_id")
    if sage_pl_id:
        try:
            pl_name = frappe.db.get_value(
                "Price List",
                {"custom_sage_price_list_id": str(sage_pl_id)},
                "name",
            )
            if pl_name:
                resolved["default_price_list"] = pl_name
        except Exception:
            pass

    if existing_name:
        doc = frappe.get_doc("Customer", existing_name)
        for field, value in resolved.items():
            if field == "custom_sage_customer_id" and not value:
                # Don't blank an existing ID on a name-only fallback match.
                continue
            doc.set(field, value)
        _apply_sales_team(doc, cust_data, sales_team_skips)
        doc.save(ignore_permissions=True)
        updated.append(cust_name or sage_id)
    else:
        if not cust_name:
            skipped.append(sage_id)
            return
        doc_dict = {"doctype": "Customer"}
        doc_dict.update(resolved)
        naming_series = safe_strip(cust_data.get("naming_series"))
        if naming_series:
            doc_dict["naming_series"] = naming_series
        new_doc = frappe.get_doc(doc_dict)
        _apply_sales_team(new_doc, cust_data, sales_team_skips)
        new_doc.insert(ignore_permissions=True)
        created.append(cust_name)


def _apply_sales_team(customer_doc, cust_data, sales_team_skips):
    """Set the customer's sales_team child table from the Sage payload.

    Each Sage `sales_team` row carries a `sales_person` name. We look
    it up in ERPNext (after stripping trailing whitespace - the Sage
    payload sometimes includes it, e.g. 'Colin Peters '). Missing
    Sales Persons are logged to `sales_team_skips` but don't block
    the customer upsert.

    Resets `customer_doc.sales_team` completely so we don't accumulate
    rows across re-pulls.
    """
    incoming_rows = cust_data.get("sales_team") or []
    customer_doc.set("sales_team", [])
    for row in incoming_rows:
        if not isinstance(row, dict):
            continue
        sp_name_raw = row.get("sales_person")
        if not sp_name_raw:
            continue
        sp_name = sp_name_raw.strip()
        if not sp_name:
            continue
        # Sales Person must exist (created by sales_person.py's pull).
        if not frappe.db.exists("Sales Person", sp_name):
            sales_team_skips.append(
                f"{customer_doc.get('customer_name')} -> {sp_name}"
            )
            continue
        customer_doc.append(
            "sales_team",
            {
                "sales_person": sp_name,
                "allocated_percentage": row.get("allocated_percentage") or 0,
                "commission_rate": row.get("commission_rate") or 0,
            },
        )


def _to_str(value):
    """Coerce a value to string, treating None and 0 as empty.

    Used by the Sage pull to normalise IDs that arrive as int (e.g. 0)

    or string. Zero is treated as 'no value' because Sage uses it as
    the unset sentinel for default IDs (price list, tax type, etc.).
    """
    if value is None or value == 0:
        return ""
    return str(value)
