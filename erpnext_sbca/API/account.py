import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import chunks, is_sync_enabled, strip_if_str


# Belt-and-suspenders safety: ERPNext core flows reference these leaf accounts
# (Stock postings, invoice rounding, GRN/SRBNB flow, write-offs, FX). Even if
# something flags one as Sage-managed or marks one Ignore in the cleanup table,
# this set keeps the sync from ever deleting them.
SYSTEM_REQUIRED_ACCOUNTS = {
    "Stock Adjustment",
    "Round Off",
    "Write Off",
    "Cost of Goods Sold",
    "Expenses Included In Valuation",
    "Expenses Included In Asset Valuation",
    "Stock In Hand",
    "Stock Received But Not Billed",
    "Service Received But Not Billed",
    "Asset Received But Not Billed",
    "Capital Work in Progress",
    "Exchange Gain/Loss",
    "Foreign Exchange Gain/Loss",
    "Cash",
    "Bank",
    "Petty Cash",
    "Accounts Receivable",
    "Accounts Payable",
}


def _ensure_sage_managed_field():
    """Idempotently create the hidden tracking flag on the Account DocType.

    The sync tags every account it creates with `custom_sage_managed = 1` so
    later runs know which accounts they own. Manually-added or pre-existing
    accounts stay un-flagged and are never touched by the sync.
    """
    if frappe.db.exists(
        "Custom Field", {"dt": "Account", "fieldname": "custom_sage_managed"}
    ):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Account",
                "fieldname": "custom_sage_managed",
                "label": "Synced From Sage",
                "fieldtype": "Check",
                "read_only": 1,
                "default": "0",
                "description": "Set by the Sage sync. Do not edit by hand.",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            message=str(e),
            title="Sage Sync: could not create custom_sage_managed field",
        )


def _apply_cleanup_choices(company_name):
    """Phase 1 strip: delete accounts the user marked Ignore in the cleanup table.

    Reads `account_cleanup_choices` rows on Erpnext Sbca Settings where the row
    matches this Company AND action == "Ignore". Each marked account is deleted
    best-effort — accounts referenced in transactions raise on delete and are
    silently kept.

    System-required accounts are guarded even if the user accidentally marked
    one Ignore (UI should hide them, but this is the defensive backstop).
    """
    choices = frappe.get_all(
        "Sage Account Cleanup Choice",
        filters={
            "parent": "Erpnext Sbca Settings",
            "parentfield": "account_cleanup_choices",
            "company": company_name,
            "action": "Ignore",
        },
        fields=["account", "account_name"],
    )
    deleted = 0
    kept = 0
    for choice in choices:
        if choice.account_name in SYSTEM_REQUIRED_ACCOUNTS:
            kept += 1
            continue
        if not frappe.db.exists("Account", choice.account):
            # Already gone — count as deleted for this run.
            deleted += 1
            continue
        try:
            frappe.delete_doc(
                "Account", choice.account, ignore_permissions=True
            )
            deleted += 1
        except Exception:
            kept += 1
    frappe.db.commit()
    frappe.logger("sbca").info(
        f"Cleanup applied for {company_name}: deleted={deleted}, kept={kept}"
    )


def get_accounts_from_sage():
    """Mirror the Sage Chart of Accounts onto each ERPNext Company.

    Two-phase model:

      Phase 1 — Setup (one-time per Company, destructive).
        Triggered when the user populates the Account Cleanup Choices table
        for a Company, then ticks Apply Account Cleanup on Next Sync on its
        Company Sage Integration row. This run deletes everything the user
        marked Ignore (best-effort), imports every Sage account fresh, and
        flips Setup Complete = 1.

      Phase 2 — Ongoing (additive, automatic).
        Once Setup Complete is true, the sync only adds Sage accounts that
        don't already exist on the Company. Nothing is ever deleted or
        modified — strict additive. Sage accounts that disappear from Sage's
        response stay in ERPNext as historical records.

    Each Sage account is created flat directly under its root (Application of
    Funds / Source of Funds / Income / Expenses / Equity), not under any
    sub-group. They're tagged custom_sage_managed = 1 so subsequent runs know
    they're Sage-owned.
    """
    if not is_sync_enabled("sync_accounts"):
        return

    _ensure_sage_managed_field()

    company_integrations = frappe.get_all(
        "Company Sage Integration", fields=["name", "company"]
    )

    for integration in company_integrations:
        company_name = integration.company
        try:
            sage = frappe.get_doc("Company Sage Integration", integration.name)

            accounts_url = (
                f"{url}/api/AccountsSync/get-accounts-for-erpnext"
                f"?apikey={sage.get_password('api_key')}&lastDate=1970-01-01"
            )
            payload = {
                "loginName": sage.username,
                "loginPwd": sage.get_password("password"),
            }
            accounts = make_post_request(accounts_url, json=payload)

            if not isinstance(accounts, list):
                frappe.log_error(
                    message=f"Unexpected API response format for {company_name}: {accounts}",
                    title=f"Sage Sync API Error for {company_name}",
                )
                continue

            # Phase 1: optional cleanup pass. Fires only when the user has
            # ticked Apply Account Cleanup on Next Sync. After it runs, the
            # flag is auto-unticked and setup_complete is set.
            if sage.get("strip_defaults_on_next_sync"):
                _apply_cleanup_choices(company_name)
                frappe.db.set_value(
                    "Company Sage Integration",
                    integration.name,
                    {
                        "strip_defaults_on_next_sync": 0,
                        "setup_complete": 1,
                    },
                )
                frappe.db.commit()

            # Cache root-account lookup by root_type for this Company.
            root_cache = {}

            def get_root(root_type):
                if root_type not in root_cache:
                    root_cache[root_type] = frappe.db.get_value(
                        "Account",
                        {
                            "company": company_name,
                            "root_type": root_type,
                            "is_group": 1,
                            "parent_account": ["in", ["", None]],
                        },
                        "name",
                    )
                return root_cache[root_type]

            # Strict additive: create each Sage account that isn't already
            # on the Company. Never delete, never modify existing accounts.
            created = 0
            skipped = 0
            for batch in chunks(accounts, 50):
                for acc_data in batch:
                    acc_name = None
                    try:
                        acc_name_raw = acc_data.get("account_name")
                        acc_name = strip_if_str(acc_name_raw) if acc_name_raw is not None else None

                        root_type_raw = acc_data.get("root_type")
                        root_type = strip_if_str(root_type_raw) if root_type_raw is not None else "Asset"

                        if not acc_name:
                            skipped += 1
                            continue

                        # Already exists — strict additive means leave alone.
                        if frappe.db.exists(
                            "Account",
                            {"account_name": acc_name, "company": company_name},
                        ):
                            skipped += 1
                            continue

                        parent_account = get_root(root_type)
                        if not parent_account:
                            frappe.log_error(
                                message=f"No root account for root_type={root_type} on {company_name}",
                                title=f"Sage Sync Root Missing for {company_name}",
                            )
                            skipped += 1
                            continue

                        acc_doc = frappe.get_doc(
                            {
                                "doctype": "Account",
                                "account_name": acc_name,
                                "company": company_name,
                                "parent_account": parent_account,
                                "root_type": root_type,
                                "is_group": 0,
                                "custom_sage_managed": 1,
                            }
                        )
                        acc_doc.insert(ignore_permissions=True)
                        created += 1

                    except Exception as inner_e:
                        frappe.log_error(
                            message=str(inner_e),
                            title=f"Error processing {acc_name or 'unknown'} [{company_name}]",
                        )
                        skipped += 1

            frappe.db.commit()
            frappe.logger("sbca").info(
                f"Sage Account Sync {company_name}: created={created}, skipped={skipped}, "
                f"setup_complete={int(bool(sage.get('setup_complete')))}"
            )

        except Exception as e:
            frappe.log_error(
                message=str(e),
                title=f"Sage Account Sync Fatal Error for {company_name}",
            )


# ---------------------------------------------------------------------------
# Whitelisted helpers — called from the Settings Client Script.
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_companies_ready_for_setup():
    """Return Company names that are eligible for the Accounts setup workflow.

    Used by the Active Company link filter on the Accounts tab. A Company is
    eligible only when BOTH of these are true:
      - A Company Sage Integration row exists for it (credentials entered).
      - At least one custom_sage_managed = 1 account exists on it (first
        successful Sage sync has completed for this Company).
    """
    integrations = frappe.get_all(
        "Company Sage Integration",
        filters={"parent": "Erpnext Sbca Settings"},
        pluck="company",
    )
    if not integrations:
        return []
    eligible = []
    for company in set(integrations):
        if frappe.db.exists(
            "Account",
            {"company": company, "custom_sage_managed": 1},
        ):
            eligible.append(company)
    return eligible


@frappe.whitelist()
def populate_eligible_accounts(company):
    """Fill the Account Cleanup Choices table with the Active Company's
    candidate accounts.

    Wipes existing rows for this Company and inserts one row per leaf account
    that:
      - lives on this Company
      - is_group = 0
      - is NOT custom_sage_managed (Sage already owns those)
      - is NOT in SYSTEM_REQUIRED_ACCOUNTS

    All rows are inserted with action = "Keep". The user then flips the ones
    they want to delete to "Ignore" and ticks Apply Account Cleanup on the
    Company Sage Integration row.

    Refuses if the Company is not yet eligible (no credentials row, or no
    first-sync success yet).
    """
    if company not in get_companies_ready_for_setup():
        frappe.throw(
            f"Company '{company}' is not yet ready. Add it to Company Sage "
            f"Integration and wait for the first successful account sync "
            f"(every ~4 minutes)."
        )

    settings = frappe.get_doc("Erpnext Sbca Settings")
    # Remove existing rows for this Company so we always start fresh.
    settings.account_cleanup_choices = [
        c for c in (settings.account_cleanup_choices or [])
        if c.company != company
    ]

    candidates = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0},
        fields=["name", "account_name", "root_type", "custom_sage_managed"],
        order_by="root_type, account_name",
    )
    added = 0
    for acc in candidates:
        if acc.custom_sage_managed:
            continue
        if acc.account_name in SYSTEM_REQUIRED_ACCOUNTS:
            continue
        settings.append(
            "account_cleanup_choices",
            {
                "company": company,
                "account": acc.name,
                "account_name": acc.account_name,
                "root_type": acc.root_type,
                "action": "Keep",
            },
        )
        added += 1

    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return {"added": added, "company": company}
