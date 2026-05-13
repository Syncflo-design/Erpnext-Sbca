app_name = "erpnext_sbca"
app_title = "Erpnext Sbca"
app_publisher = "doreen@gmail.com"
app_description = "Sage & Erpnext API"
app_email = "doreen@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "erpnext_sbca",
# 		"logo": "/assets/erpnext_sbca/logo.png",
# 		"title": "Erpnext Sbca",
# 		"route": "/erpnext_sbca",
# 		"has_permission": "erpnext_sbca.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/erpnext_sbca/css/erpnext_sbca.css"
# app_include_js = "/assets/erpnext_sbca/js/erpnext_sbca.js"

# include js, css files in header of web template
# web_include_css = "/assets/erpnext_sbca/css/erpnext_sbca.css"
# web_include_js = "/assets/erpnext_sbca/js/erpnext_sbca.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "erpnext_sbca/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
	"Item Tax Template": "public/js/item_tax_template.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "erpnext_sbca/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "erpnext_sbca.utils.jinja_methods",
# 	"filters": "erpnext_sbca.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "erpnext_sbca.install.before_install"
# after_install = "erpnext_sbca.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "erpnext_sbca.uninstall.before_uninstall"
# after_uninstall = "erpnext_sbca.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "erpnext_sbca.utils.before_app_install"
# after_app_install = "erpnext_sbca.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "erpnext_sbca.utils.before_app_uninstall"
# after_app_uninstall = "erpnext_sbca.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "erpnext_sbca.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"POS Invoice": {
		"after_submit": "erpnext_sbca.API.pos_invoice.post_pos_invoice",
	},
    "Purchase Order": {
		"after_submit": "erpnext_sbca.API.purchase_order.post_purchase_order",
        "on_cancel": "erpnext_sbca.API.cancellation.cancel_purchase_order",
	},
    "Purchase Invoice": {
		"after_submit": ["erpnext_sbca.API.purchase_invoice.post_purchase_invoice","erpnext_sbca.API.purchase_invoice.post_purchase_invoice_return"]
	},
    "Sales Order": {
		"after_submit": "erpnext_sbca.API.sales_order.post_sales_order",
        "on_cancel": "erpnext_sbca.API.cancellation.cancel_sales_order",
	},
    "Sales Invoice": {
		"after_submit": ["erpnext_sbca.API.sales_invoice.post_taxinvoice","erpnext_sbca.API.sales_invoice.post_taxinvoice_return"]
	},
    "Item": {
		"after_insert": ["erpnext_sbca.API.items.post_item"]
	},
    "Journal Entry": {
		"on_submit": "erpnext_sbca.API.journal_entry.post_journal_entry",
	},
    "Stock Entry": {
        "on_submit": "erpnext_sbca.API.stock_adjustment.post_stock_entry",
    },
    "Stock Reconciliation": {
        "on_submit": "erpnext_sbca.API.stock_adjustment.post_stock_reconciliation",
    },

}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"all": [
		"erpnext_sbca.API.sales_order.get_sales_order_from_sage",
        "erpnext_sbca.API.purchase_order.get_purchase_order_from_sage",
        "erpnext_sbca.API.supplier.get_supplier_from_sage",
        # Sales Persons must run before Customers — customer.sales_team
        # rows reference Sales Person records by name.
        "erpnext_sbca.API.sales_person.get_sales_persons_from_sage",
        # Price Lists must run before Customers (Customer.default_price_list
        # is resolved via custom_sage_price_list_id) AND before
        # get_addition_prices_from_sage (it iterates Sage pricelistIDs
        # from the Price List records).
        "erpnext_sbca.API.item_details.get_price_list_from_sage",
        "erpnext_sbca.API.customer.get_customers_from_sage",
        "erpnext_sbca.API.account.get_accounts_from_sage",
        "erpnext_sbca.API.item_details.get_item_inventory_qty_on_hand_from_sage",
        "erpnext_sbca.API.item_details.update_item_job",
        "erpnext_sbca.API.item_details.get_addition_prices_from_sage",
        "erpnext_sbca.API.item_details.update_prices",
        "erpnext_sbca.API.item_details.get_categories_from_sage",
        "erpnext_sbca.API.item_details.get_inventory_from_sage"
	],
# 	"daily": [
# 		"erpnext_sbca.tasks.daily"
# 	],
# 	"hourly": [
# 		"erpnext_sbca.tasks.hourly"
# 	],
# 	"weekly": [
# 		"erpnext_sbca.tasks.weekly"
# 	],
# 	"monthly": [
# 		"erpnext_sbca.tasks.monthly"
# 	],
}

# Testing
# -------

# before_tests = "erpnext_sbca.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "erpnext_sbca.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "erpnext_sbca.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["erpnext_sbca.utils.before_request"]
# after_request = ["erpnext_sbca.utils.after_request"]

# Job Events
# ----------
# before_job = ["erpnext_sbca.utils.before_job"]
# after_job = ["erpnext_sbca.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"erpnext_sbca.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

