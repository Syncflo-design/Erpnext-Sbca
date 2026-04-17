// Copyright (c) 2026, doreen@gmail.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Erpnext Sbca Settings", {
	refresh(frm) {
        frm.add_custom_button(__("Get Authetication Details"), function() {
            frappe.call({
                method: "erpnext_sbca.erpnext_sbca.doctype.erpnext_sbca_settings.erpnext_sbca_settings.get_authentication_details",
                callback: function(r) {
                    if (r.message) {
                    frm.refresh_fields();
                    }
                }
            });
        });
	},
});
