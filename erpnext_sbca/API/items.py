import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled
from erpnext_sbca.API.tax import build_price_pair, resolve_sage_tax

def post_item(doc, method):
    """Wrapper: enqueue the push so we don't block the Item insert transaction."""
    if not is_sync_enabled("push_item_on_insert"):
        return
    frappe.enqueue(
        "erpnext_sbca.API.items._post_item_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doc_name=doc.name,
    )


def _post_item_worker(doc_name):
    doc = frappe.get_doc("Item", doc_name)
    try:
        # Get all companies with Sage credentials
        settings = frappe.get_doc("Erpnext Sbca Settings")
        company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
        for company in company_settings:
            try:
                company = frappe.get_doc("Company Sage Integration", company.name)
                apikey = company.get_password("api_key")
                loginName = company.username
                loginPwd = company.get_password("password")
                provider = company.get_password("provider")
                session_token = company.get_password("session_id")

                # Per-tenant tax IDs come from the Item Tax Template ->
                # Sage Tax Mappings table, resolved by company. Sales rate
                # drives the price-inclusive math because the price stored
                # on the Item is the selling price.
                sales_sage_tax = resolve_sage_tax(doc, company.company, "sales")
                purchase_sage_tax = resolve_sage_tax(doc, company.company, "purchases")
                price_excl, price_incl = build_price_pair(doc, sales_sage_tax.rate)

                # Sage endpoint
                endpoint_url = f"{url}/api/InventorySync/post-new-item-to-sage?apikey={apikey}"

                # Prepare payload
                payload = {
                    "credentials": {
                        "loginName": loginName,
                        "loginPwd": loginPwd,
                        "useOAuth": bool(company.use_oauth),
                        "sessionToken": session_token,
                        "provider": provider
                    },
                    "item": {
                        "ID": 0,
                        "Code": doc.item_code,
                        "Description": doc.item_name or doc.description,
                        "Active": True if doc.disabled == 0 else False,
                        "PriceExclusive": price_excl,
                        "PriceInclusive": price_incl,
                        # Always False: every item in Sage is created as a service /
                        # "Do Not Track Balance" item. ERPNext owns stock quantities;
                        # Sage only records the financial value of stock movements via
                        # invoice pushes routed through its per-item GL mapping.
                        "Physical": False,
                        "TaxTypeIdSales": int(sales_sage_tax.sage_idx),
                        "TaxTypeIdPurchases": int(purchase_sage_tax.sage_idx),
                        "Unit": doc.stock_uom or "Each",
                        "Created": frappe.utils.format_datetime(doc.creation, "yyyy-MM-dd'T'HH:mm:ss"),
                        "Modified": frappe.utils.format_datetime(doc.modified, "yyyy-MM-dd'T'HH:mm:ss"),
                        "LastCost": float(doc.last_purchase_rate or 0),
                        "AverageCost": float(doc.valuation_rate or 0)
                    }
                }

                # Send POST request
                response = make_post_request(
                    endpoint_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )
                sage_item_id = None
                if response:
                    sage_item_id = response.get("id") or response.get("ID")
                    if sage_item_id:
                        frappe.db.set_value("Item", doc.name, "custom_sage_selection_id", str(sage_item_id))
                        
                
            except Exception as e:
                # Handle error for this company but continue with the next
                short_title = f"Sage Sync Failed for Item {doc.item_code} ({company.company})"[:140]
                error_message = str(e)

                # Try to attach response body if available
                try:
                    error_message += f"\nResponse Body: {e.response.text}"
                except Exception:
                    pass

                frappe.log_error(title=short_title, message=error_message)

    except Exception as e:
        frappe.log_error(title="Sage Inventory Sync Fatal Error"[:140], message=str(e))
