# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SageAccountOpeningBalance(Document):
    """Per-Company cache of Sage's account opening balances.

    Populated by erpnext_sbca.API.account.get_account_opening_balances_from_sage(),
    which calls /api/AccountsSync/get-accountbalances-for-erpnext on each
    Company Sage Integration row and upserts one row per account returned.

    `account_name` is stored as the raw Sage account name (e.g.
    "Unallocated Expense"), NOT the company-suffixed ERPNext name
    ("Unallocated Expense - X"). Downstream consumers resolve to ERPNext
    if they need to.

    Rows that stop being returned get disabled = 1 rather than deleted
    so historical balances stay visible.
    """

    pass
