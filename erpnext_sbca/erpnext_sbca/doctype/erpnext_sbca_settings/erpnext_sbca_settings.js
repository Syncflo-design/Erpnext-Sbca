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
