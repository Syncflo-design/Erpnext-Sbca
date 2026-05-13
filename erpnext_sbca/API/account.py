import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import chunks, is_sync_enabled, strip_if_str


# Belt-and-suspenders safety: ERPNext core flows reference these leaf accounts
# (Stock postings, invoice rounding, GRN/SRBNB flow, write-offs, FX). They're
# always kept on the Company unless Sage has a same-named equivalent, in which
# case the Sage version replaces the ERPNext default at the same name.
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
    accounts stay un-flagged.
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


def _strip_non_sage_accounts(company_name, sage_account_names):
    """Phase 1 strip: delete every non-Sage leaf account on the Company.

    Rules:
      - Sage-managed accounts (custom_sage_managed=1) are left alone.
      - System-required accounts are kept UNLESS Sage has a same-named
        equivalent (then ERPNext's is removed so Sage's takes its place at
        the same DB row name — company defaults keep resolving).
      - Anything else is deleted, best-effort. Accounts linked to a
        transaction raise on delete and are silently kept (Frappe's standard
        guard). Russell's design assumption: integration always runs against
        a fresh Company with no pre-existing transactions, so the guard
        rarely fires in practice.
    """
    candidates = frappe.get_all(
        "Account",
        filters={"company": company_name, "is_group": 0},
        fields=["name", "account_name", "custom_sage_managed"],
    )
    deleted = 0
    kept = 0
    for acc in candidates:
        if acc.custom_sage_managed:
            kept += 1
            continue
        if (
            acc.account_name in SYSTEM_REQUIRED_ACCOUNTS
            and acc.account_name not in sage_account_names
        ):
            kept += 1
            continue
        try:
            frappe.delete_doc("Account", acc.name, ignore_permissions=True)
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
        Triggered when the user ticks Apply Account Cleanup on Next Sync on a
        Company Sage Integration row. This run deletes every non-Sage non-
        system leaf account on that Company (best-effort; the link guard
        protects accounts with transactions), imports all Sage accounts
        fresh, and flips Setup Complete = 1.

      Phase 2 — Ongoing (additive, automatic).
        Once Setup Complete is true, the sync only adds Sage accounts that
        don't already exist on the Company. Nothing is ever deleted or
        modified — strict additive. Sage accounts that disappear from Sage's
        response stay in ERPNext as historical records.

    Each Sage account is created flat directly under its root (Application of
    Funds / Source of Funds / Income / Expenses / Equity), not under any
    sub-group. Tagged custom_sage_managed = 1 so subsequent runs know they're
    Sage-owned.
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

            # Build the set of Sage account names we want to keep on this Company.
            sage_account_names = set()
            for acc_data in accounts:
                acc_name_raw = acc_data.get("account_name")
                name = strip_if_str(acc_name_raw) if acc_name_raw is not None else None
                if name:
                    sage_account_names.add(name)

            # Phase 1: optional cleanup pass. Fires only when the user has
            # ticked Apply Account Cleanup on Next Sync. After it runs, the
            # flag is auto-unticked and setup_complete is set.
            if sage.get("strip_defaults_on_next_sync"):
                _strip_non_sage_accounts(company_name, sage_account_names)
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

    A Company is eligible only when BOTH of these are true:
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
def get_account_setup_status(company):
    """Return summary counts + flags for the Accounts tab status banner.

    Returns a dict with:
      - sage_managed: int — accounts already imported from Sage
      - to_be_deleted: int — non-Sage non-system leaf accounts that WOULD be
        deleted on a cleanup run (i.e., have no transactions)
      - locked: int — non-Sage non-system leaf accounts that have transactions
        and would be skipped by the link guard
      - setup_complete: bool — Phase 1 done?
      - apply_pending: bool — user has ticked Apply Account Cleanup but the
        scheduler hasn't run it yet
      - ready_for_setup: bool — credentials in place AND at least one Sage
        account has been pulled (true means the user can safely start)
    """
    if not company:
        return {}

    sage_managed = frappe.db.count(
        "Account",
        filters={"company": company, "is_group": 0, "custom_sage_managed": 1},
    )

    candidates = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0},
        fields=["name", "account_name", "custom_sage_managed"],
    )
    to_be_deleted = 0
    locked = 0
    for acc in candidates:
        if acc.custom_sage_managed:
            continue
        if acc.account_name in SYSTEM_REQUIRED_ACCOUNTS:
            # Will be kept (only replaced if Sage has same name — but that's
            # a benign rename rather than a "deletion" the user needs to see).
            continue
        if frappe.db.exists("GL Entry", {"account": acc.name}):
            locked += 1
        else:
            to_be_deleted += 1

    integration_row = frappe.db.get_value(
        "Company Sage Integration",
        {"parent": "Erpnext Sbca Settings", "company": company},
        ["name", "setup_complete", "strip_defaults_on_next_sync"],
        as_dict=True,
    )
    setup_complete = bool(integration_row and integration_row.setup_complete)
    apply_pending = bool(integration_row and integration_row.strip_defaults_on_next_sync)

    return {
        "sage_managed": sage_managed,
        "to_be_deleted": to_be_deleted,
        "locked": locked,
        "setup_complete": setup_complete,
        "apply_pending": apply_pending,
        "ready_for_setup": company in get_companies_ready_for_setup(),
    }


@frappe.whitelist()
def companies_ready_for_setup_query(doctype, txt, searchfield, start, page_len, filters):
    """Link-field set_query handler. Filters Active Company to only Companies
    that are eligible for the Accounts setup workflow.
    """
    eligible = get_companies_ready_for_setup()
    if not eligible:
        return []
    if txt:
        needle = txt.lower()
        eligible = [c for c in eligible if needle in c.lower()]
    return [[c] for c in eligible]


@frappe.whitelist()
def apply_account_cleanup(company):
    """Tick `strip_defaults_on_next_sync` on the Company's integration row.

    Called from the Client Script's "Apply Account Cleanup" button after the
    user types DELETE in the confirmation dialog. The next scheduler tick
    (or a manual Execute Now on the accounts scheduled job) does the actual
    work.
    """
    if company not in get_companies_ready_for_setup():
        frappe.throw(
            f"Company '{company}' is not yet ready for cleanup. Add it to "
            f"Company Sage Integration and wait for the first successful "
            f"account sync."
        )

    integration_name = frappe.db.get_value(
        "Company Sage Integration",
        {"parent": "Erpnext Sbca Settings", "company": company},
        "name",
    )
    if not integration_name:
        frappe.throw(
            f"No Company Sage Integration row found for '{company}'."
        )

    frappe.db.set_value(
        "Company Sage Integration",
        integration_name,
        "strip_defaults_on_next_sync",
        1,
    )
    frappe.db.commit()
    return {"queued_for": company}
