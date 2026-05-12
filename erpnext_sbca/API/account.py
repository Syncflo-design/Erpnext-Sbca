import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import chunks, is_sync_enabled, strip_if_str


# Belt-and-suspenders safety: ERPNext core flows reference these leaf accounts
# (Stock postings, invoice rounding, GRN/SRBNB flow, write-offs, FX). Even if
# something accidentally flags one as Sage-managed, this set keeps the sync
# from ever deleting them.
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
    accounts stay un-flagged and are never touched by the sync's delete pass.
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


def get_accounts_from_sage():
    """Mirror the Sage Chart of Accounts onto each ERPNext Company.

    Design (per Russell): each Company uses ONLY Sage accounts. ERPNext's
    default sub-groups (Current Assets, Fixed Assets, etc.) aren't used.
    Every Sage account is created directly under its root account
    (Application of Funds / Source of Funds / Income / Expenses / Equity)
    as a flat ledger entry.

    Each run also removes any non-group account on the Company that no longer
    appears in Sage's response, keeping the chart in sync. Deletions are
    best-effort — accounts referenced in transactions raise on delete and
    are silently kept (Frappe's standard linked-record guard).

    Idempotent: after the first sync, subsequent runs find nothing to do
    unless Sage's chart actually changed.
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

            # 1. Remove only Sage-managed accounts on this Company that Sage
            # no longer reports. Anything without the custom_sage_managed flag
            # (manual additions, ERPNext defaults, pre-existing chart) is
            # never touched.
            existing_accounts = frappe.get_all(
                "Account",
                filters={
                    "company": company_name,
                    "is_group": 0,
                    "custom_sage_managed": 1,
                },
                fields=["name", "account_name"],
            )
            deleted = 0
            kept = 0
            for existing in existing_accounts:
                if existing.account_name in sage_account_names:
                    continue
                if existing.account_name in SYSTEM_REQUIRED_ACCOUNTS:
                    # Belt-and-suspenders: never delete a system-required account
                    # even if it's somehow been flagged.
                    kept += 1
                    continue
                try:
                    frappe.delete_doc(
                        "Account", existing.name, ignore_permissions=True
                    )
                    deleted += 1
                except Exception:
                    # Linked to a transaction (or otherwise locked) — leave it.
                    kept += 1

            # 2. Cache root-account lookup by root_type for this Company.
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

            # 3. Create each Sage account directly under its root (flat).
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

                        # Already exists — leave alone.
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
                f"Sage Account Sync {company_name}: "
                f"deleted={deleted}, kept={kept}, created={created}, skipped={skipped}"
            )

        except Exception as e:
            frappe.log_error(
                message=str(e),
                title=f"Sage Account Sync Fatal Error for {company_name}",
            )
