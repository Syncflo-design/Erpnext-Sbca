# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SageReconciliationLog(Document):
    """One row per company / party / period reconciliation outcome.

    Written by erpnext_sbca.API.reconciliation. The DocType doubles as the
    idempotent guard for the daily job: before posting a reconciliation
    journal the worker checks for an existing row with status "Created" for
    the same company / party / period and skips if one is found, so the run
    is always safe to repeat.

    status:
      Created  - a reconciliation Journal Entry was posted (see journal_entry)
      Skipped  - reserved; balances already aligned / no action needed
      Failed   - an error occurred while reconciling this party
                 (see error_message)
    """

    pass
