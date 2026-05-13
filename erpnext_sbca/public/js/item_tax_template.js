// Copyright (c) 2026, Syncflo Design and contributors
// For license information, please see license.txt
//
// Item Tax Template — Sage Tax Mappings table.
//
// Filters the Sales / Purchase Sage Tax link dropdowns in each mapping
// row by the row's Company, so the user only sees Sage Tax records
// pulled for that tenant. Without this filter, every Sage Tax row from
// every Company shows in the dropdown — confusing and easy to mis-pick.
//
// Wired in hooks.py via `doctype_js`.

frappe.ui.form.on("Item Tax Template", {
    refresh(frm) {
        _wire_sage_tax_filters(frm);
    },
});

function _wire_sage_tax_filters(frm) {
    const child_field = frm.fields_dict.custom_sage_tax_map;
    if (!child_field || !child_field.grid) {
        // Custom field hasn't been created yet — run the Pull Taxes from
        // Sage button on Erpnext Sbca Settings once and reopen the form.
        return;
    }

    ["sales_sage_tax", "purchase_sage_tax"].forEach(function (field_name) {
        const link_field = child_field.grid.get_field(field_name);
        if (!link_field) return;
        link_field.get_query = function (doc, cdt, cdn) {
            const row = locals[cdt][cdn];
            // If the row's Company isn't set yet, show no rows rather than
            // every Sage Tax from every tenant.
            if (!row || !row.company) {
                return { filters: { company: "__none__" } };
            }
            return { filters: { company: row.company, disabled: 0 } };
        };
    });
}
