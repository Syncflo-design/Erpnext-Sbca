# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

"""Scheduled month-end AR/AP reconciliation against Sage.

Triggered by:
  - scheduler_events["daily"] -> erpnext_sbca.API.reconciliation.run_reconciliation

Gated by the `push_reconciliation_on_schedule` toggle on Erpnext Sbca Settings.
Defaults to OFF until Pharoh's ReconciliationSync endpoints are live.

DESIGN
------
See projects/erpnext_sbca/PAYMENT_RECONCILIATION_DESIGN.md. In short:

  Payments are NOT replicated transaction-by-transaction from Sage. Instead,
  once a day this job pulls each party's opening + closing balance from Sage
  for the period [last_reconciliation_sync .. now()] and posts a single
  Journal Entry per party that brings ERPNext's AR/AP closing balance into
  line with Sage. One journal per customer/supplier per month, not hundreds
  of individual payment entries.

Pharoh endpoints (see Pharoh_Reconciliation_Endpoint_Prompt.txt):
  POST /api/ReconciliationSync/get-customer-balances
  POST /api/ReconciliationSync/get-supplier-balances

Sage's CustomerTransactionListing / SupplierTransactionListing reports return
a per-party object that already carries OpeningBalance + ClosingBalance, plus
a nested Transactions[] detail array. Pharoh strips the Transactions detail
(ERPNext does not need it) and hands back ONLY the per-party summary for the
parties with movement in the period, shaped:
  { sageId, name, openingBalance, closingBalance }   (+ balance dates / currency)
ERPNext matches the party on sageId / name and uses closingBalance for the delta.

PER-PARTY LOGIC
---------------
  delta = Sage closing balance - ERPNext outstanding
    no ERPNext match      -> skip
    log row already exists -> skip (idempotent, safe to re-run)
    delta == 0            -> skip, nothing to align
    delta != 0            -> create + submit a reconciliation Journal Entry
                             and write a Sage Reconciliation Log row

  Customer journal (normal "customer paid in Sage" case, delta < 0):
    DR Sage Payments Clearing   CR Accounts Receivable (against the customer)
  Supplier journal (normal "we paid in Sage" case, delta < 0):
    DR Accounts Payable (against the supplier)   CR Sage Payments Clearing
  When delta runs the other way (invoice captured directly in Sage) the two
  lines reverse automatically -- see _build_je_lines.

  Reference:    SAGE-RECON-{company_abbr}-{party}-{YYYY-MM}
  Posting date: last day of the period, or today if it is the current month.

PREREQUISITES
-------------
- A Company Sage Integration row must exist for each company to reconcile.
- A leaf account named exactly "Sage Payments Clearing" must exist on each
  company (confirm the name with the accountant before the first run).
- The company must have default_receivable_account / default_payable_account
  set (ERPNext standard).
- Pharoh's ReconciliationSync endpoints must be live. Until then leave the
  push_reconciliation_on_schedule toggle OFF in Settings.

ERROR HANDLING
--------------
A Pharoh or ERPNext error for one party is logged and the run continues to
the next party -- one bad party never aborts the whole run. A company-level
failure (missing credentials, missing clearing/control account, or a failed
balances call) skips that company. `last_reconciliation_sync` is advanced
only when every company processed cleanly, so a partial run simply re-runs
next time (the log guard keeps it idempotent).
"""

import frappe
import json
from frappe.integrations.utils import make_post_request
from frappe.utils import (
    nowdate,
    now_datetime,
    getdate,
    get_last_day,
    flt,
)

url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled


# Exact account_name of the per-company clearing account. The reconciliation
# journal's contra side always posts here; it nets to zero once Sage and
# ERPNext agree. Confirm this name with the accountant before the first run.
CLEARING_ACCOUNT_NAME = "Sage Payments Clearing"

# Balances within half a cent are treated as already aligned -- no journal.
DELTA_EPSILON = 0.005

# Pharoh endpoint paths, keyed by party type.
_PHAROH_PATHS = {
    "Customer": "ReconciliationSync/get-customer-balances",
    "Supplier": "ReconciliationSync/get-supplier-balances",
}

# Custom field carrying the Sage-side id on each party doctype. Stamped by
# the customer / supplier sync from Sage's customer/supplier id. Used as the
# primary match key against the `sageId` Pharoh returns for each party.
_SAGE_ID_FIELD = {
    "Customer": "custom_sage_customer_id",
    "Supplier": "custom_sage_supplier_id",
}


# ---------------------------------------------------------------------------
# Scheduled entrypoint
# ---------------------------------------------------------------------------

def run_reconciliation():
    """scheduler_events['daily'] handler.

    Gate-checks the toggle, then hands the actual work to a background job so
    the scheduler tick is never held open while Pharoh responds. Enqueuing
    also keeps a potentially large customer/supplier list (> ~200 parties)
    off the scheduler thread.
    """
    if not is_sync_enabled("push_reconciliation_on_schedule"):
        return

    frappe.enqueue(
        "erpnext_sbca.API.reconciliation._run_reconciliation_worker",
        queue="long",
        timeout=3600,
    )


# ---------------------------------------------------------------------------
# Worker -- loops every Company Sage Integration row
# ---------------------------------------------------------------------------

def _run_reconciliation_worker():
    """Reconcile every Company Sage Integration row for one period.

    The opening date is shared across all companies in a run (it is the last
    successful run's timestamp, or the financial year start on the very first
    run). The closing date is now(). `last_reconciliation_sync` on the
    Settings doc is advanced only if every company processed without a
    company-level error.
    """
    settings = frappe.get_doc("Erpnext Sbca Settings")
    opening_date = _resolve_opening_date(settings.get("last_reconciliation_sync"))
    closing_date = getdate(nowdate())

    integrations = frappe.get_all(
        "Company Sage Integration", fields=["name", "company"]
    )
    if not integrations:
        frappe.logger("sbca").info(
            "Sage Reconciliation: no Company Sage Integration rows -- nothing to do."
        )
        return

    frappe.logger("sbca").info(
        f"Sage Reconciliation: starting run for {len(integrations)} company(ies), "
        f"period {_iso(opening_date)} .. {_iso(closing_date)}."
    )

    all_ok = True
    for ref in integrations:
        try:
            company_ok = _reconcile_company(ref.name, opening_date, closing_date)
            if not company_ok:
                all_ok = False
        except Exception as e:
            all_ok = False
            frappe.db.rollback()
            frappe.log_error(
                title=f"Sage Reconciliation: fatal error for row {ref.name}"[:140],
                message=f"Company Sage Integration row: {ref.name}\nError: {e}",
            )

    if all_ok:
        frappe.db.set_single_value(
            "Erpnext Sbca Settings", "last_reconciliation_sync", now_datetime()
        )
        frappe.db.commit()
        frappe.logger("sbca").info(
            "Sage Reconciliation: run complete -- last_reconciliation_sync advanced."
        )
    else:
        frappe.logger("sbca").info(
            "Sage Reconciliation: run finished with company-level errors -- "
            "last_reconciliation_sync NOT advanced; next run repeats this period."
        )


def _resolve_opening_date(last_sync):
    """Opening date for the period: last sync timestamp, or FY start on run 1."""
    if last_sync:
        return getdate(last_sync)
    try:
        # get_fiscal_year lives in erpnext.accounts.utils (Fiscal Year is an
        # ERPNext doctype). Lazy-imported so a module-load never depends on
        # erpnext, and wrapped so a missing/ambiguous fiscal year falls back.
        from erpnext.accounts.utils import get_fiscal_year

        return getdate(get_fiscal_year(nowdate(), as_dict=True).year_start_date)
    except Exception:
        # Last-resort fallback: 1 Jan of the current calendar year.
        return getdate(f"{getdate(nowdate()).year}-01-01")


# ---------------------------------------------------------------------------
# Per-company processing
# ---------------------------------------------------------------------------

def _reconcile_company(integration_name, opening_date, closing_date):
    """Reconcile one company's customers and suppliers.

    Returns True if both party types were fetched and processed without a
    company-level error (individual party errors are logged and skipped, and
    do NOT flip this to False). Returns False on a company-level problem
    (missing credentials or clearing/control account, or a failed Pharoh
    call) so the caller knows not to advance the shared timestamp.

    Follows the per-company credential pattern from stock_adjustment.py.
    """
    integration = frappe.get_doc("Company Sage Integration", integration_name)
    company = integration.company

    if not company:
        frappe.log_error(
            title="Sage Reconciliation: integration row has no Company"[:140],
            message=(
                f"Company Sage Integration row {integration_name} has no "
                f"Company set -- skipped."
            ),
        )
        return False

    apikey = integration.get_password("api_key")
    if not apikey:
        frappe.log_error(
            title=f"Sage Reconciliation: missing API key for '{company}'"[:140],
            message=(
                f"No API key on the Company Sage Integration row for "
                f"'{company}'. Fill it in under Erpnext Sbca Settings -> "
                f"Connection, then re-run."
            ),
        )
        return False

    clearing_account = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "account_name": CLEARING_ACCOUNT_NAME,
            "is_group": 0,
        },
        "name",
    )
    if not clearing_account:
        frappe.log_error(
            title=f"Sage Reconciliation: no clearing account for '{company}'"[:140],
            message=(
                f"No leaf account named '{CLEARING_ACCOUNT_NAME}' found on "
                f"company '{company}'. Create it (confirm the exact name with "
                f"the accountant) before reconciliation can run for this "
                f"company."
            ),
        )
        return False

    # Standard credentials block -- identical shape to stock_adjustment.py.
    credentials = {
        "loginName": integration.username,
        "loginPwd": integration.get_password("password"),
        "useOAuth": bool(integration.use_oauth),
        "sessionToken": integration.get_password("session_id"),
        "provider": integration.get_password("provider"),
    }

    company_ok = True
    for party_type in ("Customer", "Supplier"):
        try:
            ok = _reconcile_party_type(
                party_type=party_type,
                company=company,
                clearing_account=clearing_account,
                credentials=credentials,
                apikey=apikey,
                opening_date=opening_date,
                closing_date=closing_date,
            )
            if not ok:
                company_ok = False
        except Exception as e:
            company_ok = False
            frappe.db.rollback()
            frappe.log_error(
                title=(
                    f"Sage Reconciliation: {party_type} run failed for "
                    f"'{company}'"
                )[:140],
                message=(
                    f"Company: {company}\nParty type: {party_type}\nError: {e}"
                ),
            )

    return company_ok


def _reconcile_party_type(
    party_type, company, clearing_account, credentials, apikey,
    opening_date, closing_date,
):
    """Pull balances for one party type from Pharoh and reconcile each party.

    Returns True if the Pharoh call succeeded and the list was processed;
    False on a company-level failure (bad/empty Pharoh response, or the
    company has no default receivable/payable account). Per-party errors are
    logged and skipped -- they do not flip the return value.
    """
    # Control account this party type posts against.
    control_field = (
        "default_receivable_account"
        if party_type == "Customer"
        else "default_payable_account"
    )
    party_account = frappe.get_cached_value("Company", company, control_field)
    if not party_account:
        frappe.log_error(
            title=(
                f"Sage Reconciliation: no control account for {party_type} "
                f"on '{company}'"
            )[:140],
            message=(
                f"Company '{company}' has no {control_field} set. Set the "
                f"default {'receivable' if party_type == 'Customer' else 'payable'} "
                f"account on the Company record, then re-run."
            ),
        )
        return False

    # Fetch the balances list from Pharoh.
    payload = {
        "credentials": credentials,
        "openingDate": _iso(opening_date),
        "closingDate": _iso(closing_date),
    }
    endpoint_url = f"{url}/api/{_PHAROH_PATHS[party_type]}?apikey={apikey}"

    try:
        response = make_post_request(
            endpoint_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
    except Exception as http_err:
        body = ""
        try:
            body = http_err.response.text
        except Exception:
            body = str(http_err)
        frappe.log_error(
            title=(
                f"Sage Reconciliation: Pharoh HTTP error ({party_type}) "
                f"for '{company}'"
            )[:140],
            message=(
                f"Company: {company}\nParty type: {party_type}\n"
                f"Endpoint: {endpoint_url}\nError: {http_err}\n"
                f"Response body: {body[:1500]}\n"
                f"Payload: {json.dumps(payload)[:1500]}"
            ),
        )
        return False

    if not isinstance(response, list):
        frappe.log_error(
            title=(
                f"Sage Reconciliation: bad Pharoh response ({party_type}) "
                f"for '{company}'"
            )[:140],
            message=(
                f"Expected a JSON array of party balances, got "
                f"{type(response).__name__}: {str(response)[:1000]}"
            ),
        )
        return False

    period = _iso(closing_date)[:7]  # "YYYY-MM"
    posting_date = _posting_date_for_period(closing_date)
    company_abbr = frappe.get_cached_value("Company", company, "abbr") or company

    created = skipped = failed = 0
    for entry in response:
        if not isinstance(entry, dict):
            continue
        try:
            outcome = _reconcile_one_party(
                entry=entry,
                party_type=party_type,
                company=company,
                company_abbr=company_abbr,
                party_account=party_account,
                clearing_account=clearing_account,
                period=period,
                posting_date=posting_date,
                upto_date=closing_date,
            )
            if outcome == "created":
                created += 1
            else:
                skipped += 1
            frappe.db.commit()
        except Exception as e:
            failed += 1
            frappe.db.rollback()
            party_name = str(
                entry.get("name") or entry.get("sageId") or "<unknown>"
            )
            matched = (
                party_name
                if party_name and frappe.db.exists(party_type, party_name)
                else None
            )
            _safe_log_failure(
                company=company,
                party_type=party_type,
                party=matched,
                period=period,
                posting_date=posting_date,
                error_message=str(e),
            )
            frappe.log_error(
                title=(
                    f"Sage Reconciliation: {party_type} '{party_name}' failed "
                    f"({company})"
                )[:140],
                message=(
                    f"Company: {company}\nParty type: {party_type}\n"
                    f"Sage entry: {json.dumps(entry)[:1000]}\nError: {e}"
                ),
            )
            continue

    frappe.logger("sbca").info(
        f"Sage Reconciliation [{company} / {party_type}]: "
        f"{len(response)} returned by Sage -- "
        f"created={created}, skipped={skipped}, failed={failed}."
    )
    return True


def _reconcile_one_party(
    entry, party_type, company, company_abbr, party_account,
    clearing_account, period, posting_date, upto_date,
):
    """Reconcile a single customer/supplier. Returns an outcome string.

    Outcomes: "created" (journal posted + log written) or "skipped" (no
    ERPNext match, an existing journal for this company/party/period, or a
    zero delta). Raises on any error so the caller can roll back and record
    a Failed log row.
    """
    # Sage returns a numeric sageId and a display name per party. sageId may
    # arrive as an int (e.g. 356693) -- coerce to a trimmed string for matching.
    party_name = str(entry.get("name") or "").strip()
    sage_id = str(entry.get("sageId") or "").strip()

    party = _match_party(party_type, party_name, sage_id)
    if not party:
        frappe.logger("sbca").info(
            f"Sage Reconciliation [{company} / {party_type}]: no ERPNext match "
            f"for Sage party '{party_name or sage_id}' -- skipped."
        )
        return "skipped"

    # Idempotent guard -- a reconciliation journal already exists for this
    # company/party/period. Safe to re-run; just move on. Only "Created" rows
    # block re-processing, so a prior "Failed" row is still allowed to retry.
    if frappe.db.exists(
        "Sage Reconciliation Log",
        {
            "company": company,
            "party_type": party_type,
            "party": party,
            "period": period,
            "status": "Created",
        },
    ):
        frappe.logger("sbca").info(
            f"Sage Reconciliation [{company} / {party_type}]: '{party}' already "
            f"reconciled for {period} -- skipped."
        )
        return "skipped"

    sage_closing = flt(entry.get("closingBalance"))
    erpnext_outstanding = _erpnext_outstanding(
        company, party_type, party, upto_date
    )
    delta = flt(sage_closing - erpnext_outstanding, 2)

    if abs(delta) < DELTA_EPSILON:
        frappe.logger("sbca").info(
            f"Sage Reconciliation [{company} / {party_type}]: '{party}' already "
            f"aligned for {period} (delta 0) -- skipped."
        )
        return "skipped"

    reference = f"SAGE-RECON-{company_abbr}-{party}-{period}"
    remark = (
        f"Sage payment reconciliation -- {party_type} {party} -- {period}. "
        f"Sage closing {sage_closing:.2f} vs ERPNext outstanding "
        f"{erpnext_outstanding:.2f} (delta {delta:.2f})."
    )

    accounts = _build_je_lines(
        party_type=party_type,
        party=party,
        party_account=party_account,
        clearing_account=clearing_account,
        delta=delta,
    )

    je = frappe.get_doc(
        {
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": company,
            "posting_date": posting_date,
            # cheque_no doubles as the Sage reconciliation reference; the
            # Journal Entry push hook skips any JE whose reference starts
            # with "SAGE-RECON-" so these never loop back to Sage.
            "cheque_no": reference,
            "cheque_date": posting_date,
            "user_remark": remark,
            "accounts": accounts,
        }
    )
    je.insert(ignore_permissions=True)
    je.submit()

    frappe.get_doc(
        {
            "doctype": "Sage Reconciliation Log",
            "company": company,
            "party_type": party_type,
            "party": party,
            "period": period,
            "sage_closing_balance": sage_closing,
            "erpnext_outstanding": erpnext_outstanding,
            "delta": delta,
            "journal_entry": je.name,
            "status": "Created",
            "error_message": "",
            "reconciliation_date": posting_date,
        }
    ).insert(ignore_permissions=True)

    frappe.logger("sbca").info(
        f"Sage Reconciliation [{company} / {party_type}]: '{party}' {period} -- "
        f"posted {je.name} (delta {delta:.2f})."
    )
    return "created"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_party(party_type, party_name, sage_id):
    """Resolve a Sage party to an ERPNext Customer/Supplier name.

    Sage's transaction listing identifies each party by a numeric `sage_id`
    (Sage's customer/supplier id) and a display `party_name`. The customer /
    supplier sync stamps that Sage id onto custom_sage_customer_id /
    custom_sage_supplier_id, so it is the primary, stable match key. The
    plain document-name match is the fallback for ERPNext records that
    pre-date the Sage sync and so carry no Sage id yet.
    """
    sage_id_field = _SAGE_ID_FIELD[party_type]

    if sage_id:
        try:
            match = frappe.db.get_value(
                party_type, {sage_id_field: sage_id}, "name"
            )
            if match:
                return match
        except Exception:
            # Field may not exist yet on a site that has never run the
            # customer/supplier sync -- fall through to the name match.
            pass

    if party_name and frappe.db.exists(party_type, party_name):
        return party_name
    return None


def _erpnext_outstanding(company, party_type, party, upto_date):
    """ERPNext outstanding balance for a party, as a positive 'amount owed'.

    Customers: receivable is a debit-balance, so SUM(debit) - SUM(credit) is
    already the positive amount the customer owes us.
    Suppliers: payable is a credit-balance, so the sign is flipped to give
    the positive amount we owe the supplier -- matching Sage's closing-
    balance convention so the delta maths is the same for both party types.
    """
    rows = frappe.db.sql(
        """
        SELECT COALESCE(SUM(debit) - SUM(credit), 0)
        FROM `tabGL Entry`
        WHERE company = %(company)s
          AND party_type = %(party_type)s
          AND party = %(party)s
          AND is_cancelled = 0
          AND posting_date <= %(upto_date)s
        """,
        {
            "company": company,
            "party_type": party_type,
            "party": party,
            "upto_date": _iso(upto_date),
        },
    )
    raw = flt(rows[0][0]) if rows and rows[0] else 0.0
    return raw if party_type == "Customer" else -raw


def _build_je_lines(party_type, party, party_account, clearing_account, delta):
    """Build the two Journal Entry account rows for a reconciliation journal.

    `delta` = Sage closing balance - ERPNext outstanding (signed, non-zero).

    The party-side control account is moved by exactly `delta` so ERPNext's
    closing balance lands on Sage's:

      Customer, delta < 0  (customer paid in Sage -- the common case):
        DR Sage Payments Clearing   CR Accounts Receivable (against customer)
      Customer, delta > 0  (invoice captured directly in Sage):
        DR Accounts Receivable (against customer)   CR Sage Payments Clearing

      Supplier, delta < 0  (we paid the supplier in Sage -- the common case):
        DR Accounts Payable (against supplier)   CR Sage Payments Clearing
      Supplier, delta > 0  (bill captured directly in Sage):
        DR Sage Payments Clearing   CR Accounts Payable (against supplier)

    The clearing account always nets to zero across all parties once Sage
    and ERPNext are in sync.
    """
    amount = abs(delta)

    party_line = {
        "account": party_account,
        "party_type": party_type,
        "party": party,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": 0,
    }
    clearing_line = {
        "account": clearing_account,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": 0,
    }

    if party_type == "Customer":
        # Receivable is a debit-balance account: move it by +delta.
        if delta > 0:
            party_line["debit_in_account_currency"] = amount
            clearing_line["credit_in_account_currency"] = amount
        else:
            party_line["credit_in_account_currency"] = amount
            clearing_line["debit_in_account_currency"] = amount
    else:
        # Payable is a credit-balance account: move 'amount owed' by +delta.
        if delta > 0:
            party_line["credit_in_account_currency"] = amount
            clearing_line["debit_in_account_currency"] = amount
        else:
            party_line["debit_in_account_currency"] = amount
            clearing_line["credit_in_account_currency"] = amount

    return [party_line, clearing_line]


def _posting_date_for_period(closing_date):
    """Last day of the closing date's month, or today if it's the current month."""
    closing = getdate(closing_date)
    today = getdate(nowdate())
    last_day = getdate(get_last_day(closing))
    return today if last_day > today else last_day


def _iso(value):
    """Coerce a date / datetime / string to an ISO 'YYYY-MM-DD' string."""
    return getdate(value).strftime("%Y-%m-%d")


def _safe_log_failure(
    company, party_type, party, period, posting_date, error_message,
):
    """Best-effort write of a Failed Sage Reconciliation Log row.

    Called after a per-party exception has already been rolled back. Wrapped
    in its own try/except so a logging failure never masks the original
    error. `party` may be None when the Sage party could not be matched to
    an ERPNext record.
    """
    try:
        frappe.get_doc(
            {
                "doctype": "Sage Reconciliation Log",
                "company": company,
                "party_type": party_type,
                "party": party,
                "period": period,
                "sage_closing_balance": 0,
                "erpnext_outstanding": 0,
                "delta": 0,
                "status": "Failed",
                "error_message": (error_message or "")[:500],
                "reconciliation_date": posting_date,
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
