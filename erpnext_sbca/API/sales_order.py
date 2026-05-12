import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled

def post_sales_order(doc, method):
    """Wrapper: enqueue the push so we don't block the Sales Order submit transaction."""
    if not is_sync_enabled("push_sales_order_on_submit"):
        return
    frappe.enqueue(
        "erpnext_sbca.API.sales_order._post_sales_order_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doc_name=doc.name,
    )


def _post_sales_order_worker(doc_name):
    doc = frappe.get_doc("Sales Order", doc_name)
    payload = {}

    try:

        # 1. Prevent duplicate sync
        if doc.get("custom_sage_order_id"):
            frappe.throw("Sales Order already synced to Sage.")

        # 2. Get Sage credentials
        settings = frappe.get_doc("Erpnext Sbca Settings")
        company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
        for company in company_settings:
            company = frappe.get_doc("Company Sage Integration", company.name)
            apikey = company.get_password("api_key")
            loginName = company.username
            loginPwd = company.get_password("password")
            provider = company.get_password("provider")
            session_token = company.get_password("session_id")
            if not apikey or not loginName or not loginPwd:
                frappe.throw("Sage credentials missing in Sage Integration.")

            endpoint_url = f"{url}/api/SalesOrder/post-salesorder-to-sage?apikey={apikey}"

            # 3. Validate Customer
            customer_doc = frappe.get_doc("Customer", doc.customer)
            sage_customer_id = customer_doc.get("custom_sage_customer_id")

            if not sage_customer_id:
                frappe.throw(f"Sage Customer ID missing on Customer: {doc.customer}")

            try:
                customer_id = int(sage_customer_id)
            except:
                frappe.throw("Sage Customer ID must be numeric.")

            # 3b. Validate Sales Rep
            sage_sales_rep_id = doc.get("custom_sage_sales_rep_id")
            if not sage_sales_rep_id:
                frappe.throw("Sage Sales Rep ID missing on Sales Order. Please fill in the 'Sage Sales Rep ID' field.")

            try:
                sales_rep_id = int(sage_sales_rep_id)
            except:
                frappe.throw("Sage Sales Rep ID must be numeric.")

            # 4. Get Tax Rate
            tax_rate = 0.0
            if doc.taxes:
                for tax_row in doc.taxes:
                    if tax_row.charge_type == "On Net Total":
                        tax_rate = float(tax_row.rate or 0)
                        break

            # 5. Build Lines
            lines = []

            if not doc.items:
                frappe.throw("No items found in Sales Order.")

            for item in doc.items:

                item_doc = frappe.get_doc("Item", item.item_code)
                selection_raw = item_doc.get("custom_sage_selection_id")

                if not selection_raw:
                    frappe.throw(
                        f"Sage Selection ID missing for item: {item.item_code}. "
                        f"Please sync this item to Sage first."
                    )

                try:
                    selection_id = int(float(str(selection_raw).strip()))
                except:
                    frappe.throw(f"Sage Selection ID must be numeric for item: {item.item_code}")

                tax_type_id = int(item_doc.get("tax_typeid_sales") or item_doc.get("custom_sage_tax_type_id") or 0)

                item_exclusive = float(item.net_amount or item.amount or 0)
                item_rate_excl = float(item.net_rate or item.rate or 0)
                item_rate_incl = float(item.rate or 0)
                item_tax = round(item_exclusive * (tax_rate / 100), 2)
                item_total = round(item_exclusive + item_tax, 2)

                lines.append({
                    "selectionId": selection_id,
                    "id": 0,
                    "lineType": 0,
                    "description": item.description or item.item_name or "",
                    "unit": item.uom or "",
                    "comments": "",
                    "quantity": float(item.qty or 0),
                    "unitPriceExclusive": item_rate_excl,
                    "unitPriceInclusive": item_rate_incl,
                    "unitCost": float(item.valuation_rate or item.net_rate or item.rate or 0),
                    "exclusive": item_exclusive,
                    "discount": float(item.discount_amount or 0),
                    "discountPercentage": float(item.discount_percentage or 0),
                    "tax": item_tax,
                    "total": item_total,
                    "taxPercentage": round(tax_rate / 100, 4),
                    "taxTypeId": tax_type_id
                })

            # 6. Build Payload
            payload = {
                "credentials": {
                    "loginName": loginName,
                    "loginPwd": loginPwd,
                    "useOAuth": bool(company.use_oauth),
                    "sessionToken": session_token,
                    "provider": provider
                },
                "order": {
                    "id": 0,
                    "date": frappe.utils.formatdate(doc.transaction_date, "yyyy-MM-dd") + "T00:00:00",
                    "deliveryDate": frappe.utils.formatdate(doc.delivery_date, "yyyy-MM-dd") + "T00:00:00" if doc.delivery_date else frappe.utils.formatdate(doc.transaction_date, "yyyy-MM-dd") + "T00:00:00",
                    "customer": {
                        "id": customer_id
                    },
                    "salesRepresentative": {
                        "id": sales_rep_id
                    },
                    "reference": doc.name or "",
                    "message": doc.remarks or "",
                    "tax": float(doc.total_taxes_and_charges or 0),
                    "discount": float(doc.discount_amount or 0),
                    "total": float(doc.grand_total or 0),
                    "lines": lines
                }
            }

            # 7. Send Request
            sage_response_text = "No response captured"

            try:
                response = make_post_request(
                    endpoint_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )

                if response and response.get("success"):
                    doc.db_set("custom_sage_order_id", str(response.get("sageOrderId") or ""))
                    doc.db_set("custom_sage_document_number", str(response.get("documentNumber") or ""))
                    try:
                        doc.db_set("custom_sage_sync_status", "Synced")
                    except Exception:
                        pass
                else:
                    error_msg = response.get("errorMessage") or str(response) if response else "Unknown"
                    frappe.throw(f"Sage API Error: {error_msg}")

            except Exception as http_err:
                err_str = str(http_err)
                sage_body = ""
                try:
                    sage_body = http_err.response.text
                except Exception:
                    sage_body = err_str

                frappe.log_error(
                    message=f"HTTP Error: {err_str}\nSage Response Body: {sage_body}\nPayload: {str(payload)}",
                    title=f"Sage API HTTP Error - {doc.name}"
                )
                frappe.throw(f"Sage API Error: {sage_body}")

    except Exception as e:
        frappe.log_error(
            message=f"Sales Order: {doc.name}\nCustomer: {doc.customer}\nError: {str(e)}\nPayload: {str(payload)}",
            title="Sage Sales Order Sync Error"
        )
        try:
            doc.db_set("custom_sage_sync_status", "Failed")
        except Exception:
            pass
        frappe.throw(f"Sage Sync Failed: {str(e)}")



@frappe.whitelist()
def get_sales_order_from_sage():
    if not is_sync_enabled("sync_sales_orders"):
        return
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
            last_date = frappe.utils.add_days(frappe.utils.today(), -30)  # last 30 days

            so_url = f"{url}/api/SalesOrder/get-salesorders-for-erpnext?apikey={apikey}&lastDate={last_date}"
            payload = {"loginName": loginName, "loginPwd": loginPwd, "useOAuth": bool(company.use_oauth),
            "sessionToken": session_token,
            "provider": provider}

            # API call with debug
            sales_orders = None
            try:
                debug_start = f"API Start {company.company}: {frappe.utils.now()} URL={so_url[:50]}..."[:140]
                frappe.log_error(debug_start, "Sage SO Sync Debug")
                sales_orders = make_post_request(so_url, json=payload)
                debug_resp = f"API Resp {company.company}: {len(sales_orders) if sales_orders else 'None'} items"[:140]
                frappe.log_error(debug_resp, "Sage SO Sync Debug")
            except Exception as api_e:
                api_err_title = f"API Fail {company.company}: {str(api_e)}"[:140]
                frappe.log_error(api_err_title, "Sage SO Sync Error")
                continue

            if not isinstance(sales_orders, list):
                invalid_title = f"Invalid Resp {company.company}: not list"[:140]
                frappe.log_error(invalid_title, "Sage SO Sync")
                continue

            # Limit to 50 SOs per run
            sales_orders = sales_orders[:50]

            updated_sos = []
            created_sos = []
            skipped_sos = []

            # Caches
            uom_cache = set()
            stock_uom_cache = set()
            item_cache = set()
            group_cache = set(["Sage Imported Items"])
            warehouse_cache = {}  # Cache for warehouses

            batch_size = 20

            for i in range(0, len(sales_orders), batch_size):
                batch = sales_orders[i:i + batch_size]
                for so_data in batch:
                    so_name = (so_data.get("name") or "").strip() if isinstance(so_data.get("name"), str) else so_data.get("name", "")
                    customer_name = (so_data.get("customer_name") or "").strip() if isinstance(so_data.get("customer_name"), str) else so_data.get("customer_name", "")

                    if not so_name or not customer_name:
                        skipped_sos.append(so_name)
                        continue

                    try:
                        so_filter = {"custom_sage_name": so_name, "company": company.company}
                        is_update = False

                        if frappe.db.exists("Sales Order", so_filter):
                            so_doc = frappe.get_doc("Sales Order", so_filter)
                            is_update = True
                            so_doc.set("items", [])
                        else:
                            so_doc = frappe.new_doc("Sales Order")
                            so_doc.naming_series = "SAL-ORD-.YYYY.-"
                            so_doc.custom_sage_name = so_name
                            so_doc.company = company.company

                        # Direct field assignments
                        so_doc.customer = customer_name
                        so_doc.transaction_date = frappe.utils.getdate(so_data.get("transaction_date"))
                        so_doc.delivery_date = frappe.utils.getdate(so_data.get("delivery_date"))
                        so_doc.total = so_data.get("total", 0)
                        so_doc.total_taxes_and_charges = so_data.get("total_taxes_and_charges", 0)
                        so_doc.discount_amount = so_data.get("discount_amount", 0)

                        for item in so_data.get("items", []):
                            # Cached UOM
                            uom_name = (item.get("uom") or "Nos").strip() if isinstance(item.get("uom"), str) else (item.get("uom") or "Nos")
                            if not uom_name:
                                uom_name = "Nos"
                            if uom_name not in uom_cache:
                                if not frappe.db.exists("UOM", uom_name):
                                    uom_doc = frappe.new_doc("UOM")
                                    uom_doc.uom_name = uom_name
                                    uom_doc.insert(ignore_permissions=True)
                                uom_cache.add(uom_name)
                            uom_val = uom_name

                            # Cached Stock UOM
                            stock_uom_name = (item.get("stock_uom") or uom_val).strip() if isinstance(item.get("stock_uom"), str) else (item.get("stock_uom") or uom_val)
                            if not stock_uom_name:
                                stock_uom_name = uom_val
                            if stock_uom_name not in stock_uom_cache:
                                if not frappe.db.exists("UOM", stock_uom_name):
                                    stock_uom_doc = frappe.new_doc("UOM")
                                    stock_uom_doc.uom_name = stock_uom_name
                                    stock_uom_doc.insert(ignore_permissions=True)
                                stock_uom_cache.add(stock_uom_name)
                            stock_uom_val = stock_uom_name

                            # Cached Item Group
                            item_group_name = "Sage Imported Items"
                            if item_group_name not in group_cache:
                                if not frappe.db.exists("Item Group", item_group_name):
                                    ig_doc = frappe.new_doc("Item Group")
                                    ig_doc.item_group_name = item_group_name
                                    ig_doc.parent_item_group = "All Item Groups"
                                    ig_doc.insert(ignore_permissions=True)
                                group_cache.add(item_group_name)

                            # Cached Item
                            item_code_param = (item.get("code") or "").strip() if isinstance(item.get("code"), str) else item.get("code", "")
                            item_name_param = (item.get("item_name") or "").strip() if isinstance(item.get("item_name"), str) else item.get("item_name", "")
                            description_param = (item.get("description") or "").strip() if isinstance(item.get("description"), str) else item.get("description", "")
                            if not item_code_param:
                                item_code_param = item_name_param or "ITEM-" + frappe.generate_hash("", 5)
                            if not item_name_param:
                                item_name_param = item_code_param
                            if not description_param:
                                description_param = item_name_param

                            if item_code_param not in item_cache:
                                if not frappe.db.exists("Item", item_code_param):
                                    item_doc = frappe.new_doc("Item")
                                    item_doc.item_code = item_code_param
                                    item_doc.item_name = item_name_param
                                    item_doc.description = description_param
                                    item_doc.stock_uom = stock_uom_val
                                    item_doc.is_stock_item = 1
                                    item_doc.item_group = item_group_name
                                    item_doc.insert(ignore_permissions=True)
                                item_cache.add(item_code_param)

                            item_code = item_code_param

                            # --- Warehouse Handling ---
                            raw_warehouse = item.get("warehouse") or ""
                            if not raw_warehouse:
                                base_warehouse_name = "All Warehouses"
                            else:
                                base_warehouse_name = raw_warehouse.strip() if isinstance(raw_warehouse, str) else str(raw_warehouse)

                            # ERPNext auto adds company suffix (first letter capitalized)
                            company_suffix = company.company[0].upper()
                            final_warehouse_name = f"{base_warehouse_name} - {company_suffix}"

                            # Check cache first
                            if final_warehouse_name in warehouse_cache:
                                warehouse_to_use = warehouse_cache[final_warehouse_name]
                            else:
                                existing_wh = frappe.db.exists("Warehouse", final_warehouse_name)
                                if existing_wh:
                                    warehouse_to_use = final_warehouse_name
                                else:
                                    wh_doc = frappe.new_doc("Warehouse")
                                    wh_doc.warehouse_name = base_warehouse_name  # ERPNext adds suffix automatically
                                    wh_doc.company = company.company
                                    wh_doc.insert(ignore_permissions=True)
                                    warehouse_to_use = final_warehouse_name

                                warehouse_cache[final_warehouse_name] = warehouse_to_use

                            # Append item
                            so_doc.append("items", {
                                "item_code": item_code,
                                "item_name": item_name_param,
                                "description": description_param,
                                "qty": item.get("qty") or 0,
                                "uom": uom_val,
                                "stock_uom": stock_uom_val,
                                "conversion_factor": item.get("conversion_factor", 1),
                                "rate": item.get("rate", 0),
                                "amount": item.get("amount", 0),
                                "discount_percentage": item.get("discount_percentage", 0),
                                "discount_amount": item.get("discount_amount", 0),
                                "warehouse": warehouse_to_use,
                                "cost_center": item.get("cost_center") or "",
                                "project": item.get("project") or ""
                            })

                        so_doc.save(ignore_permissions=True)

                        if is_update:
                            updated_sos.append(so_name)
                        else:
                            created_sos.append(so_name)

                    except Exception as e:
                        proc_err_title = f"SO Proc Err {so_name}: {str(e)}"[:140]
                        frappe.log_error(proc_err_title)
                        skipped_sos.append(so_name)

                # Commit after each batch
                frappe.db.commit()

            # Final commit for the integration
            frappe.db.commit()

            summary_title = f"Sage SO Summary {company.company}"[:140]
            summary = f"Company: {company.company} | Processed {len(sales_orders)} SOs | Updated: {len(updated_sos)}, Created: {len(created_sos)}, Skipped: {len(skipped_sos)}"
            frappe.log_error(summary, summary_title)

        except Exception as e:
            fatal_title = f"Sage SO Fatal Err {company.company}: {str(e)}"[:140]
            frappe.log_error(fatal_title)
