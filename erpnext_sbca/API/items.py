import frappe

def post_item(doc,method):
    try:
        # Collect results for all companies
        sync_results = []

        # Get all companies with Sage credentials
        companies = frappe.get_all("Company Sage Integration", fields=["name", "company"])

        for company_doc in companies:
            try:
                # Get credentials for this company
                sage = frappe.get_doc("Company Sage Integration", company_doc.name)
                apikey = sage.get_password("api_key")
                loginName = sage.username
                loginPwd = sage.get_password("password")
                tax_id=sage.tax_id

                # Sage endpoint
                url = f"https://pharoh.co.za/api/InventorySync/post-new-item-to-sage?apikey={apikey}"

                # Prepare payload
                payload = {
                    "credentials": {
                        "loginName": loginName,
                        "loginPwd": loginPwd
                    },
                    "item": {
                        "ID": 0,
                        "Code": doc.item_code,
                        "Description": doc.item_name or doc.description,
                        "Active": True if doc.disabled == 0 else False,
                        "PriceExclusive": float(doc.standard_rate or 0),
                        "PriceInclusive": float(doc.standard_rate or 0) * 1.15,  # adjust VAT if needed
                        "Physical": True if doc.is_stock_item else False,
                        "TaxTypeIdSales": tax_id,
                        "TaxTypeIdPurchases": tax_id,
                        "Unit": doc.stock_uom or "Each",
                        "Created": frappe.utils.format_datetime(doc.creation, "yyyy-MM-dd'T'HH:mm:ss"),
                        "Modified": frappe.utils.format_datetime(doc.modified, "yyyy-MM-dd'T'HH:mm:ss"),
                        "LastCost": float(doc.last_purchase_rate or 0),
                        "AverageCost": float(doc.valuation_rate or 0)
                    }
                }

                # Send POST request
                response = frappe.make_post_request(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )
                sage_item_id = None
                if response:
                    sage_item_id = response.get("id") or response.get("ID")
                    if sage_item_id:
                        frappe.db.set_value("Item", doc.name, "custom_sage_selection_id", str(sage_item_id))
                        
                
                sync_results.append(
                    f"✅ Sage Sync Success for <b>{doc.item_code}</b> ({sage.company})<br>Sage ID: {sage_item_id}"
                )

            except Exception as e:
                # Handle error for this company but continue with the next
                short_title = f"Sage Sync Failed for Item {doc.item_code} ({company_doc.company})"[:140]
                error_message = str(e)

                # Try to attach response body if available
                try:
                    error_message += f"\nResponse Body: {e.response.text}"
                except Exception:
                    pass

                frappe.log_error(title=short_title, message=error_message)

                sync_results.append(
                    f"❌ Failed to sync {doc.item_code} for {company_doc.company}<br>Error: "
                )

        # Show all results in a single message
        if sync_results:
            frappe.msgprint("<br><br>".join(sync_results))

    except Exception as e:
        frappe.log_error(title="Sage Inventory Sync Fatal Error"[:140], message=str(e))
