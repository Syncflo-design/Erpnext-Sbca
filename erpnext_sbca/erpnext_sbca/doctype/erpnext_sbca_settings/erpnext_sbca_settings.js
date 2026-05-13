// Copyright (c) 2026, Syncflo and contributors
// For license information, please see license.txt

frappe.ui.form.on("Erpnext Sbca Settings", {
    refresh(frm) {
        // Existing: Sage OAuth handshake helper.
        frm.add_custom_button(__("Get Authetication Details"), function () {
            frappe.call({
                method: "erpnext_sbca.erpnext_sbca.doctype.erpnext_sbca_settings.erpnext_sbca_settings.get_authentication_details",
                callback: function (r) {
                    if (r.message) {
                        frm.refresh_fields();
                    }
                },
            });
        });

        // Phase B — Accounts tab UI.
        _filter_active_company(frm);
        _render_accounts_status(frm);
        _add_apply_cleanup_button(frm);

        // Sage Taxes — on-demand pull of the per-Company tax catalogue.
        _add_pull_taxes_button(frm);
        _render_taxes_status(frm);

        // Opening Balances — on-demand pull of Sage account opening balances.
        _add_pull_opening_balances_button(frm);
        _render_opening_balances_status(frm);
    },

    active_company(frm) {
        _render_accounts_status(frm);
    },
});

// ---------------------------------------------------------------------------
// Active Company link is restricted to Companies that have a Sage Integration
// row AND at least one Sage-managed account. Prevents the user from kicking
// off a cleanup against a Company whose first Sage sync hasn't landed yet.
// ---------------------------------------------------------------------------
function _filter_active_company(frm) {
    frm.set_query("active_company", function () {
        return {
            query: "erpnext_sbca.API.account.companies_ready_for_setup_query",
        };
    });
}

// ---------------------------------------------------------------------------
// Render the Accounts tab status banner for the selected Company.
// ---------------------------------------------------------------------------
function _render_accounts_status(frm) {
    const company = frm.doc.active_company;
    const wrapper = frm.fields_dict.accounts_intro;
    if (!wrapper) return;

    if (!company) {
        const helpHtml = `
            <div style="color:#6c757d;font-size:12px;line-height:1.5;margin:8px 0;">
                <p>Pick an <b>Active Company</b> above to see its account setup status.</p>
                <p>Only Companies that have a Sage Integration row <i>and</i>
                at least one successful Sage account sync appear in the picker.</p>
            </div>`;
        frm.set_df_property("accounts_intro", "options", helpHtml);
        frm.refresh_field("accounts_intro");
        return;
    }

    frappe.call({
        method: "erpnext_sbca.API.account.get_account_setup_status",
        args: { company: company },
        callback: function (r) {
            if (!r.message) return;
            const s = r.message;
            const phaseLabel = s.setup_complete
                ? `<span style="color:#1b7a3a;">Phase 2 — strict additive</span>`
                : `<span style="color:#a05d00;">Phase 1 — setup not yet applied</span>`;
            const pendingLabel = s.apply_pending
                ? `<span style="color:#a05d00;"><br>(Apply Account Cleanup is queued — runs on the next sync tick)</span>`
                : "";
            const html = `
                <div style="font-size:13px;line-height:1.5;margin:8px 0;padding:12px;background:#f8f9fa;border-radius:6px;">
                    <div style="font-size:14px;font-weight:600;margin-bottom:6px;">${frappe.utils.escape_html(company)}</div>
                    <div>State: ${phaseLabel}${pendingLabel}</div>
                    <div style="margin-top:8px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">
                        <div><span style="color:#6c757d;">Sage-managed accounts:</span> <b>${s.sage_managed}</b></div>
                        <div><span style="color:#6c757d;">Would be deleted on cleanup:</span> <b>${s.to_be_deleted}</b></div>
                        <div><span style="color:#6c757d;">Locked (have transactions):</span> <b>${s.locked}</b></div>
                    </div>
                    <div style="color:#6c757d;font-size:11px;margin-top:8px;">
                        <b>Apply Account Cleanup</b> deletes every non-Sage non-system leaf account on this Company, then imports Sage's chart. System-required accounts (Stock Adjustment, Round Off, COGS, etc.) are protected. Accounts with transactions are skipped by Frappe's standard guard.
                    </div>
                </div>`;
            frm.set_df_property("accounts_intro", "options", html);
            frm.refresh_field("accounts_intro");
        },
    });
}

// ---------------------------------------------------------------------------
// Apply Account Cleanup — typed-DELETE confirmation, then queues the run.
// ---------------------------------------------------------------------------
function _add_apply_cleanup_button(frm) {
    frm.add_custom_button(
        __("Apply Account Cleanup"),
        function () {
            const company = frm.doc.active_company;
            if (!company) {
                frappe.msgprint(__("Pick an Active Company in the Accounts tab first."));
                return;
            }

            const d = new frappe.ui.Dialog({
                title: __("Confirm Account Cleanup"),
                fields: [
                    {
                        fieldtype: "HTML",
                        options: `
                            <div style="font-size:13px;line-height:1.5;">
                                <p>You are about to delete every <b>non-Sage non-system leaf account</b>
                                on <b>${frappe.utils.escape_html(company)}</b>. This cannot be undone.</p>
                                <p>The next Sage account-sync run (every ~4 minutes) will:</p>
                                <ol>
                                    <li>Delete the non-Sage accounts on this Company (best-effort — accounts with transactions are kept).</li>
                                    <li>Import all Sage accounts fresh.</li>
                                    <li>Flip Setup Complete to true. From then on, only additive — nothing else is deleted.</li>
                                </ol>
                                <p>Type <b>DELETE</b> (all caps) below to confirm.</p>
                            </div>`,
                    },
                    {
                        fieldtype: "Data",
                        fieldname: "confirm",
                        label: __("Type DELETE"),
                        reqd: 1,
                    },
                ],
                primary_action_label: __("Queue Cleanup"),
                primary_action(values) {
                    if (values.confirm !== "DELETE") {
                        frappe.msgprint(__("Type exactly DELETE (uppercase) to proceed."));
                        return;
                    }
                    frappe.call({
                        method: "erpnext_sbca.API.account.apply_account_cleanup",
                        args: { company: company },
                        callback: function (r) {
                            if (r.message) {
                                frappe.show_alert({
                                    message: __(
                                        "Cleanup queued for {0}. The next sync (≤4 min) will apply it.",
                                        [company]
                                    ),
                                    indicator: "green",
                                });
                                d.hide();
                                frm.reload_doc();
                            }
                        },
                    });
                },
            });
            d.show();
        },
        __("Account Setup")
    );
}

// ---------------------------------------------------------------------------
// Pull Taxes from Sage — refreshes the Sage Tax catalogue for every
// Company Sage Integration row. Taxes change rarely, so this is button-only
// (no scheduled task). The button is the single source of truth for the
// Sage Tax records that the push paths read at runtime.
// ---------------------------------------------------------------------------
function _add_pull_taxes_button(frm) {
    frm.add_custom_button(
        __("Pull Taxes from Sage"),
        function () {
            frappe.show_alert({
                message: __("Pulling tax catalogue from Sage…"),
                indicator: "blue",
            });
            frappe.call({
                method: "erpnext_sbca.API.tax.get_taxes_from_sage",
                freeze: true,
                freeze_message: __("Talking to Sage…"),
                callback: function (r) {
                    if (!r.message) {
                        frappe.msgprint(
                            __("Sage tax pull returned no result — check the error log.")
                        );
                        return;
                    }
                    const summaries = r.message;
                    if (summaries.length === 0) {
                        frappe.msgprint(
                            __("No Company Sage Integration rows configured.")
                        );
                        return;
                    }

                    const rows = summaries
                        .map(function (s) {
                            const safe = frappe.utils.escape_html;
                            const errBlock =
                                s.errors && s.errors.length
                                    ? `<div style="color:#a00;font-size:11px;margin-top:4px;">` +
                                      s.errors
                                          .map((e) => safe(e))
                                          .join("<br>") +
                                      `</div>`
                                    : "";
                            return `
                                <tr>
                                    <td style="padding:4px 8px;">${safe(s.company)}</td>
                                    <td style="padding:4px 8px;text-align:right;"><b>${s.created}</b></td>
                                    <td style="padding:4px 8px;text-align:right;"><b>${s.updated}</b></td>
                                    <td style="padding:4px 8px;text-align:right;"><b>${s.disabled}</b></td>
                                    <td style="padding:4px 8px;text-align:right;color:${
                                        s.errors && s.errors.length ? "#a00" : "inherit"
                                    };"><b>${s.errors ? s.errors.length : 0}</b>${errBlock}</td>
                                </tr>`;
                        })
                        .join("");

                    frappe.msgprint({
                        title: __("Sage Tax Pull Summary"),
                        message: `
                            <div style="font-size:13px;">
                                <table style="width:100%;border-collapse:collapse;">
                                    <thead>
                                        <tr style="background:#f0f0f0;">
                                            <th style="padding:6px 8px;text-align:left;">Company</th>
                                            <th style="padding:6px 8px;text-align:right;">Created</th>
                                            <th style="padding:6px 8px;text-align:right;">Updated</th>
                                            <th style="padding:6px 8px;text-align:right;">Disabled</th>
                                            <th style="padding:6px 8px;text-align:right;">Errors</th>
                                        </tr>
                                    </thead>
                                    <tbody>${rows}</tbody>
                                </table>
                                <p style="color:#6c757d;font-size:11px;margin-top:8px;">
                                    Next step: open each Item Tax Template and set its
                                    <b>Sage Tax Mappings</b> table — one row per Company,
                                    picking the appropriate sales and purchase Sage tax
                                    records.
                                </p>
                            </div>`,
                        wide: true,
                    });
                    // Refresh the Taxes-tab status banner after a successful pull.
                    _render_taxes_status(frm);
                },
            });
        },
        __("Sage Taxes")
    );
}

// ---------------------------------------------------------------------------
// Taxes tab status banner — counts Sage Tax records per Company and shows
// the most recent pull timestamp. Re-rendered after a successful pull.
// ---------------------------------------------------------------------------
function _render_taxes_status(frm) {
    const wrapper = frm.fields_dict.taxes_intro;
    if (!wrapper) return;

    frappe.call({
        method: "erpnext_sbca.API.tax.get_tax_status",
        callback: function (r) {
            if (!r.message) return;
            const summaries = r.message;
            const html = _build_status_table_html({
                title: "Sage Tax Catalogue",
                summaries: summaries,
                empty_message:
                    "No Company Sage Integration rows configured. " +
                    "Set one up on the Connection tab.",
                no_data_message:
                    "No Sage Tax records yet for this Company. " +
                    "Click <b>Pull Taxes from Sage</b> above.",
                value_columns: [
                    { key: "active", label: "Active", align: "right" },
                    { key: "disabled", label: "Disabled", align: "right" },
                    {
                        key: "last_seen_at",
                        label: "Last Pull",
                        align: "right",
                        format: "datetime",
                    },
                ],
                footer:
                    "<b>Pull Taxes from Sage</b> refreshes this table. " +
                    "Open each Item Tax Template and set its <b>Sage Tax Mappings</b> " +
                    "child table to wire items to the appropriate Sage records " +
                    "per Company.",
            });
            frm.set_df_property("taxes_intro", "options", html);
            frm.refresh_field("taxes_intro");
        },
    });
}

// ---------------------------------------------------------------------------
// Pull Opening Balances from Sage — on-demand button, mirrors the Pull
// Taxes pattern. Refreshes the Opening Balances tab status banner on
// success.
// ---------------------------------------------------------------------------
function _add_pull_opening_balances_button(frm) {
    frm.add_custom_button(
        __("Pull Opening Balances"),
        function () {
            frappe.show_alert({
                message: __("Pulling opening balances from Sage…"),
                indicator: "blue",
            });
            frappe.call({
                method:
                    "erpnext_sbca.API.account.get_account_opening_balances_from_sage",
                freeze: true,
                freeze_message: __("Talking to Sage…"),
                callback: function (r) {
                    if (!r.message) {
                        frappe.msgprint(
                            __(
                                "Sage opening-balance pull returned no result " +
                                    "— check the error log."
                            )
                        );
                        return;
                    }
                    const summaries = r.message;
                    if (summaries.length === 0) {
                        frappe.msgprint(
                            __("No Company Sage Integration rows configured.")
                        );
                        return;
                    }

                    const rows = summaries
                        .map(function (s) {
                            const safe = frappe.utils.escape_html;
                            const errBlock =
                                s.errors && s.errors.length
                                    ? `<div style="color:#a00;font-size:11px;margin-top:4px;">` +
                                      s.errors
                                          .map((e) => safe(e))
                                          .join("<br>") +
                                      `</div>`
                                    : "";
                            return `
                                <tr>
                                    <td style="padding:4px 8px;">${safe(s.company)}</td>
                                    <td style="padding:4px 8px;text-align:right;"><b>${s.created}</b></td>
                                    <td style="padding:4px 8px;text-align:right;"><b>${s.updated}</b></td>
                                    <td style="padding:4px 8px;text-align:right;"><b>${s.disabled}</b></td>
                                    <td style="padding:4px 8px;text-align:right;color:${
                                        s.errors && s.errors.length ? "#a00" : "inherit"
                                    };"><b>${s.errors ? s.errors.length : 0}</b>${errBlock}</td>
                                </tr>`;
                        })
                        .join("");

                    frappe.msgprint({
                        title: __("Sage Opening Balance Pull Summary"),
                        message: `
                            <div style="font-size:13px;">
                                <table style="width:100%;border-collapse:collapse;">
                                    <thead>
                                        <tr style="background:#f0f0f0;">
                                            <th style="padding:6px 8px;text-align:left;">Company</th>
                                            <th style="padding:6px 8px;text-align:right;">Created</th>
                                            <th style="padding:6px 8px;text-align:right;">Updated</th>
                                            <th style="padding:6px 8px;text-align:right;">Disabled</th>
                                            <th style="padding:6px 8px;text-align:right;">Errors</th>
                                        </tr>
                                    </thead>
                                    <tbody>${rows}</tbody>
                                </table>
                            </div>`,
                        wide: true,
                    });
                    _render_opening_balances_status(frm);
                },
            });
        },
        __("Opening Balances")
    );
}

// ---------------------------------------------------------------------------
// Opening Balances tab status banner — counts opening balance records per
// Company plus the active total value.
// ---------------------------------------------------------------------------
function _render_opening_balances_status(frm) {
    const wrapper = frm.fields_dict.opening_balances_intro;
    if (!wrapper) return;

    frappe.call({
        method: "erpnext_sbca.API.account.get_opening_balance_status",
        callback: function (r) {
            if (!r.message) return;
            const summaries = r.message;
            const html = _build_status_table_html({
                title: "Sage Account Opening Balances",
                summaries: summaries,
                empty_message:
                    "No Company Sage Integration rows configured. " +
                    "Set one up on the Connection tab.",
                no_data_message:
                    "No opening balance records yet for this Company. " +
                    "Click <b>Pull Opening Balances</b> above.",
                value_columns: [
                    { key: "active", label: "Active", align: "right" },
                    { key: "disabled", label: "Disabled", align: "right" },
                    {
                        key: "total_value",
                        label: "Total (active)",
                        align: "right",
                        format: "currency",
                    },
                    {
                        key: "last_seen_at",
                        label: "Last Pull",
                        align: "right",
                        format: "datetime",
                    },
                ],
                footer:
                    "Run the <b>Apply Account Cleanup</b> + account sync first " +
                    "(see the Accounts tab) so Sage's accounts are mirrored " +
                    "into ERPNext before pulling balances.",
            });
            frm.set_df_property("opening_balances_intro", "options", html);
            frm.refresh_field("opening_balances_intro");
        },
    });
}

// ---------------------------------------------------------------------------
// Shared renderer for both Taxes and Opening Balances status banners.
// `opts.value_columns` defines which fields to render and how to format them.
// ---------------------------------------------------------------------------
function _build_status_table_html(opts) {
    const safe = frappe.utils.escape_html;
    if (!opts.summaries || opts.summaries.length === 0) {
        return `
            <div style="font-size:13px;line-height:1.5;margin:8px 0;padding:12px;background:#f8f9fa;border-radius:6px;">
                <div style="font-size:14px;font-weight:600;margin-bottom:6px;">${safe(opts.title)}</div>
                <div style="color:#6c757d;">${opts.empty_message}</div>
            </div>`;
    }

    const headers = opts.value_columns
        .map(function (c) {
            const align = c.align || "left";
            return `<th style="padding:6px 8px;text-align:${align};">${safe(c.label)}</th>`;
        })
        .join("");

    const rows = opts.summaries
        .map(function (s) {
            const cells = opts.value_columns
                .map(function (c) {
                    const align = c.align || "left";
                    let value = s[c.key];
                    if (value === null || value === undefined) {
                        value = `<span style="color:#a05d00;">${safe(opts.no_data_message)}</span>`;
                    } else if (c.format === "currency") {
                        value = `<b>${format_currency(value, frappe.defaults.get_default("currency") || "")}</b>`;
                    } else if (c.format === "datetime") {
                        value = frappe.datetime.str_to_user(value);
                    } else {
                        value = `<b>${value}</b>`;
                    }
                    return `<td style="padding:6px 8px;text-align:${align};vertical-align:top;">${value}</td>`;
                })
                .join("");
            return `<tr><td style="padding:6px 8px;font-weight:600;">${safe(s.company)}</td>${cells}</tr>`;
        })
        .join("");

    return `
        <div style="font-size:13px;line-height:1.5;margin:8px 0;padding:12px;background:#f8f9fa;border-radius:6px;">
            <div style="font-size:14px;font-weight:600;margin-bottom:8px;">${safe(opts.title)}</div>
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="background:#eef0f3;">
                        <th style="padding:6px 8px;text-align:left;">Company</th>
                        ${headers}
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
            <div style="color:#6c757d;font-size:11px;margin-top:8px;">${opts.footer}</div>
        </div>`;
}
