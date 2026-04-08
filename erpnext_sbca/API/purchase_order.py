import frappe
import json

payload = {}

def convert_timestamp(ts):
    return ts.isoformat()

def post_purchase_order(doc,method): 
    try:
        sage = frappe.get_doc("Sage Integration", {"company": doc.company})
        apikey = sage.get_password("api_key")
        loginName = sage.username
        loginPwd = sage.get_password("password")

        if not apikey or not loginName or not loginPwd:
            frappe.throw("Sage credentials missing in Sage Integration.")

        url = f"https://pharoh.co.za/api/PurchaseOrder/post-purchaseorder-to-sage?apikey={apikey}"

        supplier_doc = frappe.get_doc("Supplier", doc.supplier)
        sage_supplier_id = supplier_doc.get("custom_sage_supplier_id")
        supplier_id = 0
        if not sage_supplier_id:
            frappe.throw(f"Sage Supplier ID missing on supplier: {doc.supplier}")
        try:
            supplier_id = int(sage_supplier_id)
        except:
            frappe.throw("Sage Supplier ID must be numeric.")


        if not doc.items:
            frappe.throw("No items found in Sales Invoice.")
        lines = []
        for item in doc.items:
            item_doc = frappe.get_doc("Item", item.item_code)
            selection_raw = item_doc.get("custom_sage_selection_id")
            if not selection_raw:
                frappe.throw(

                    f"Sage Selection ID missing for item: {item.item_code}. "

                    f"Please sync this item to Sage first."

                )
            try:
                selection_raw = int(selection_raw)
            except:
                frappe.throw("Sage Supplier ID must be numeric.")
            tax_type_id = int(item_doc.get("tax_typeid_sales") or item_doc.get("custom_sage_tax_type_id") or 0)
            item_exclusive = float(item.net_amount or item.amount or 0)
            item_rate_excl = float(item.net_rate or item.rate or 0)
            item_rate_incl = float(item.rate or 0)
            item_tax = 0
            tax_details = doc.item_wise_tax_details
            if tax_details != []:
                for tax in tax_details:
                    if item.name == tax.get("item_row"):
                        item_tax = tax.get("amount")
            item_total = item.base_amount
            lines.append ({
                
            "selectionId": selection_raw,
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
            "tax": item_tax,
            "total": item_total,
            "taxPercentage": 0,
            "discountPercentage": float(item.discount_percentage or 0),
            "taxTypeId": tax_type_id,
            "currencyId": 0,
            "analysisCategoryId1": 0,
            "analysisCategoryId2": 0,
            "analysisCategoryId3": 0
                
                })
    
        payload = {

            "credentials": {

                "loginName": loginName,

                "loginPwd": loginPwd

            },
            "order": {
            "id": 0,
            "documentNumber": doc.name,
            "date": convert_timestamp(doc.creation),
            "deliveryDate": convert_timestamp(doc.creation),
            "status": "Yes", # not sure
            "supplier": {
                "id": supplier_id,
                "name": "",
                "taxReference": "",
                "contactName": "",
                "telephone": "",
                "fax": "",
                "mobile": "",
                "email": "",
                "webAddress": "",
                "active": True,
                "isObfuscated": True,
                "balance": 0,
                "creditLimit": 0,
                "postalAddress01": doc.billing_address,
                "postalAddress02": doc.billing_address,
                "postalAddress03": doc.billing_address,
                "postalAddress04": doc.billing_address,
                "postalAddress05": doc.billing_address,
                "deliveryAddress01": doc.billing_address,
                "deliveryAddress02": doc.billing_address,
                "deliveryAddress03": doc.billing_address,
                "deliveryAddress04": doc.billing_address,
                "deliveryAddress05": doc.billing_address,
                "autoAllocateToOldestInvoice": True,
                "textField1": "",
                "textField2": "",
                "textField3": "",
                "numericField1": 0,
                "numericField2": 0,
                "numericField3": 0,
                "yesNoField1": True,
                "yesNoField2": True,
                "yesNoField3": True,
                "dateField1": convert_timestamp(doc.creation),
                "dateField2": convert_timestamp(doc.creation),
                "dateField3": convert_timestamp(doc.creation),
                "accountingAgreement": True,
                "hasSpecialCountryTaxActivity": True,
                "modified": convert_timestamp(doc.modified),
                "created": convert_timestamp(doc.creation),
                "businessRegistrationNumber": "",
                "rmcdApprovalNumber": "",
                "taxStatusVerified": convert_timestamp(doc.creation),
                "currencyId": 0,
                "currencySymbol": "",
                "hasActivity": True,
                "defaultDiscountPercentage": 0,
                "defaultTaxTypeId": 0,
                "dueDateMethodId": 0,
                "dueDateMethodValue": 0,
                "subjectToDRCVat": True
                },
                "total": float(doc.grand_total or 0),
                "tax": float(doc.total_taxes_and_charges or 0),
                "discount": float(doc.discount_amount or 0),
                "reference": doc.name or "",
                "message": doc.remarks or "",
                "lines": lines,
                "addresses": [
                {
                    "addressType": "",
                    "line1": "",
                    "line2": "",
                    "city": "",
                    "province": "",
                    "postalCode": "",
                    "country": ""
                }
                ],
                "inclusive": True,
                "discountPercentage": float(doc.additional_discount_percentage or 0),
                "taxReference": "",
                "exclusive": float(doc.grand_total or 0),
                "rounding": float(doc.rounding_adjustment or 0),
                "amountDue": float(doc.outstanding_amount or 0),
                "externalReference": "",
                "supplier_CurrencyId": 0,
                "supplier_ExchangeRate": 0,
                "useForeignCurrency": True,
                "postalAddress01": "doc.billing_address",
                "postalAddress02": "doc.billing_address",
                "postalAddress03": "doc.billing_address",
                "postalAddress04": "doc.billing_address",
                "postalAddress05": "doc.billing_address",
                "deliveryAddress01": "doc.billing_address",
                "deliveryAddress02": "doc.billing_address",
                "deliveryAddress03": "doc.billing_address",
                "deliveryAddress04": "doc.billing_address",
                "deliveryAddress05": "doc.billing_address"
            }
        }
        
        sage_response_text = "No response captured"
        payload = json.dumps(payload)
        
        try:
            response = frappe.make_post_request(

                url,

                data=payload,

                headers={"Content-Type": "application/json"}

            )
            if response and response.get("success"):

                doc.db_set("custom_sage_order_id", str(response.get("sageOrderId") or ""))

                doc.db_set("custom_sage_document_number", str(response.get("documentNumber") or ""))

                try:

                    doc.db_set("custom_sage_sync_status", "Synced")

                except Exception:

                    pass

                frappe.msgprint(

                    f"✅ Sage Sync Successful!\n"

                    f"Sage Order ID: {response.get('sageOrderId')}\n"

                    f"Document Number: {response.get('documentNumber')}"

                )

            else:

                error_msg = response.get("errorMessage") or str(response) if response else "Unknown"

                frappe.throw(f"Sage API Error: {error_msg}")

        except Exception as http_err:
            frappe.log_error(str(http_err), "SupplierReturn API Error")
            raise

    except Exception as e:

        frappe.log_error(

            message=f"Purchase Order: {doc.name}\nCustomer: {doc.supplier}\nError: {str(e)}\nPayload: {str(payload)}",

            title="Sage Purchase Order Sync Error"

        )
        try:
            doc.db_set("custom_sage_sync_status", "Failed")

        except Exception:

            pass

        frappe.throw(f"Sage Sync Failed: {str(e)}")

    finally:
        doc.reload() 

   
@frappe.whitelist()
def get_purchase_order_from_sage():
    company_integrations = frappe.get_all("Company Sage Integration", fields=["name", "company"])

    for integration in company_integrations:
        try:
            sage = frappe.get_doc("Company Sage Integration", integration.name)
            apikey = sage.get_password("api_key")
            loginName = sage.username
            loginPwd = sage.get_password("password")
            last_date = frappe.utils.add_days(frappe.utils.today(), -30)  # Dynamic: last 7 days

            po_url = f"https://pharoh.co.za/api/PurchaseOrder/get-purchaseorders-for-erpnext?apikey={apikey}&lastDate={last_date}"
            payload = {"loginName": loginName, "loginPwd": loginPwd}

            # Enhanced API call with exception handling
            purchase_orders = None
            try:
                debug_start = f"API Start {sage.company}: {frappe.utils.now()} URL={po_url[:50]}..."[:140]
                frappe.log_error(debug_start, "Sage PO Sync Debug")
                purchase_orders = frappe.make_post_request(po_url, json=payload)
                debug_resp = f"API Resp {sage.company}: {len(purchase_orders) if purchase_orders else 'None'} items"[:140]
                frappe.log_error(debug_resp, "Sage PO Sync Debug")
            except Exception as api_e:
                api_err_title = f"API Fail {sage.company}: {str(api_e)}"[:140]
                frappe.log_error(api_err_title, "Sage PO Sync Error")
                continue  # Skip to next integration

            if not isinstance(purchase_orders, list):
                invalid_title = f"Invalid Resp {sage.company}: not list"[:140]
                frappe.log_error(invalid_title, "Sage PO Sync")
                continue

            # Limit to 50 POs per run to prevent timeout
            purchase_orders = purchase_orders[:50]

            updated_pos = []
            created_pos = []
            skipped_pos = []

            # Caches to reduce DB queries
            uom_cache = set()
            stock_uom_cache = set()
            item_cache = set()
            group_cache = set(["Sage Imported Items"])  # Pre-add common group

            batch_size = 20

            for i in range(0, len(purchase_orders), batch_size):
                batch = purchase_orders[i:i + batch_size]
                for po_data in batch:
                    po_name = (po_data.get("name") or "").strip() if isinstance(po_data.get("name"), str) else po_data.get("name", "")
                    supplier_name = (po_data.get("supplier_name") or "").strip() if isinstance(po_data.get("supplier_name"), str) else po_data.get("supplier_name", "")

                    if not po_name or not supplier_name:
                        skipped_pos.append(po_name)
                        continue

                    try:
                        po_filter = {"custom_sage_name": po_name, "company": sage.company}
                        is_update = False

                        if frappe.db.exists("Purchase Order", po_filter):
                            po_doc = frappe.get_doc("Purchase Order", po_filter)
                            is_update = True
                            po_doc.set("items", [])
                        else:
                            po_doc = frappe.new_doc("Purchase Order")
                            po_doc.naming_series = "PUR-ORD-.YYYY.-"
                            po_doc.custom_sage_name = po_name
                            po_doc.company = sage.company

                        # Direct field assignments
                        po_doc.supplier = supplier_name
                        po_doc.transaction_date = frappe.utils.getdate(po_data.get("transaction_date"))
                        po_doc.schedule_date = frappe.utils.getdate(po_data.get("delivery_date"))
                        po_doc.total = po_data.get("total", 0)
                        po_doc.total_taxes_and_charges = po_data.get("total_taxes_and_charges", 0)
                        po_doc.discount_amount = po_data.get("discount_amount", 0)

                        for item in po_data.get("items", []):
                            # Cached UOM check/create
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

                            # Cached stock_uom
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

                            # Cached Item check/create
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

                            po_doc.append("items", {
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
                                "warehouse": item.get("warehouse") or "",
                                "cost_center": item.get("cost_center") or "",
                                "project": item.get("project") or ""
                            })

                        po_doc.save(ignore_permissions=True)

                        if is_update:
                            updated_pos.append(po_name)
                        else:
                            created_pos.append(po_name)

                    except Exception as e:
                        proc_err_title = f"PO Proc Err {po_name}: {str(e)}"[:140]
                        frappe.log_error(proc_err_title)
                        skipped_pos.append(po_name)

                # Commit after each batch to persist changes
                frappe.db.commit()

            # Final commit for the integration
            frappe.db.commit()

            summary_title = f"Sage PO Summary {sage.company}"[:140]
            summary = f"Company: {sage.company} | Processed {len(purchase_orders)} POs | Updated: {len(updated_pos)}, Created: {len(created_pos)}, Skipped: {len(skipped_pos)}"
            frappe.log_error(summary, summary_title)

        except Exception as e:
            fatal_title = f"Sage Fatal Err {integration.company}: {str(e)}"[:140]
            frappe.log_error(fatal_title)