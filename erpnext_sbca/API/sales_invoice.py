import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled

def post_taxinvoice(doc, method):
    """Wrapper: enqueue the push so we don't block the Sales Invoice submit transaction."""
    if not is_sync_enabled("push_sales_invoice_on_submit"):
        return
    frappe.enqueue(
        "erpnext_sbca.API.sales_invoice._post_taxinvoice_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doc_name=doc.name,
    )


def _post_taxinvoice_worker(doc_name):
    doc = frappe.get_doc("Sales Invoice", doc_name)
    payload = {}

    # Skip if return invoice or POS invoice (handled by other workers)
    if doc.get("is_return"):
        return

    elif doc.get("is_pos"):
        return

    else:
        
        try:
            
            # 1. Prevent duplicate sync
            if doc.get("custom_sage_order_id"):
                frappe.throw("Invoice already synced to Sage.")
        
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
            
                endpoint_url = f"{url}/api/TaxInvoice/post-taxinvoice-to-sage?apikey={apikey}"
            
                # 3. Validate Customer
                customer_doc = frappe.get_doc("Customer", doc.customer)
                sage_customer_id = customer_doc.get("custom_sage_customer_id")
            
                if not sage_customer_id:
                    frappe.throw(f"Sage Customer ID missing on Customer: {doc.customer}")
            
                try:
                    customer_id = int(sage_customer_id)
                except:
                    frappe.throw("Sage Customer ID must be numeric.")
            
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
                    frappe.throw("No items found in Sales Invoice.")
            
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
            
                # 6. Invoice level totals
                invoice_exclusive = float(doc.net_total or 0)
                invoice_tax = float(doc.total_taxes_and_charges or 0)
                invoice_total = float(doc.grand_total or 0)
                invoice_rounding = float(doc.rounding_adjustment or 0)
            
                payload = {
                    "credentials": {
                        "loginName": loginName,
                        "loginPwd": loginPwd,
                        "useOAuth": bool(company.use_oauth),
                        "sessionToken": session_token,
                        "provider": provider

                    },
                    "invoice": {
                        "date": frappe.utils.formatdate(doc.posting_date, "yyyy-MM-dd"),
                        "inclusive": False,
                        "discountPercentage": float(doc.additional_discount_percentage or 0),
                        "taxReference": "",
                        "customerName": doc.customer_name or "",
                        "customerId": customer_id,
                        "dueDate": frappe.utils.formatdate(doc.due_date, "yyyy-MM-dd") if doc.due_date else frappe.utils.formatdate(doc.posting_date, "yyyy-MM-dd"),
                        "reference": doc.name or "",
                        "message": doc.remarks or "",
                        "discount": float(doc.discount_amount or 0),
                        "exclusive": invoice_exclusive,
                        "tax": invoice_tax,
                        "rounding": invoice_rounding,
                        "total": invoice_total,
                        "amountDue": float(doc.outstanding_amount or invoice_total),
                        "lines": lines
                    }
                }
            
                # 7. Send Request
                # Use make_post_request but catch the raw response via the session object
                # that frappe exposes through frappe.local
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
            
                    # make_post_request raises the HTTPError directly from requests
                    # The response object is stored on the exception as .response
                    # RestrictedPython blocks underscore vars but allows attribute access
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
                message=f"Sales Invoice: {doc.name}\nCustomer: {doc.customer}\nError: {str(e)}\nPayload: {str(payload)}",
                title="Sage Sales Invoice Sync Error"
            )
            try:
                doc.db_set("custom_sage_sync_status", "Failed")
            except Exception:
                pass
            frappe.throw(f"Sage Sync Failed: {str(e)}")

def post_taxinvoice_return(doc, method):
    """Wrapper: enqueue the push so we don't block the Sales Invoice (return) submit transaction."""
    if not is_sync_enabled("push_sales_invoice_return_on_submit"):
        return
    frappe.enqueue(
        "erpnext_sbca.API.sales_invoice._post_taxinvoice_return_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doc_name=doc.name,
    )


def _post_taxinvoice_return_worker(doc_name):
    doc = frappe.get_doc("Sales Invoice", doc_name)
    payload = {}

    if doc.get("is_return"):
        
        try:
        
            # 2. Prevent duplicate sync
            if doc.get("custom_sage_sync_status") == "Synced":
                frappe.throw("Credit Note already synced to Sage.")
        
            # 3. Get original invoice details
            return_against = doc.get("return_against")
            if not return_against:
                frappe.throw("Return Against (original invoice) is missing on this Credit Note.")
        
            original_invoice = frappe.get_doc("Sales Invoice", return_against)
            from_document_id = int(original_invoice.get("custom_sage_order_id") or 0)
            from_document_number = original_invoice.get("custom_sage_document_number") or ""
        
            if not from_document_id:
                frappe.throw(f"Original invoice {return_against} has no Sage Order ID. Please sync it first.")
        
            # 4. Get Sage credentials
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
            
                endpoint_url = f"{url}/api/CustomerReturn/post-customerreturn-to-sage?apikey={apikey}"
            
                # 5. Validate Customer
                customer_doc = frappe.get_doc("Customer", doc.customer)
                sage_customer_id = customer_doc.get("custom_sage_customer_id")
            
                if not sage_customer_id:
                    frappe.throw(f"Sage Customer ID missing on Customer: {doc.customer}")
            
                try:
                    customer_id = int(sage_customer_id)
                except:
                    frappe.throw("Sage Customer ID must be numeric.")
            
                # 6. Get Tax Rate
                tax_rate = 0.0
                if doc.taxes:
                    for tax_row in doc.taxes:
                        if tax_row.charge_type == "On Net Total":
                            tax_rate = float(tax_row.rate or 0)
                            break
            
                # 7. Build Lines (use abs() for all values since Credit Note has negatives)
                lines = []
            
                if not doc.items:
                    frappe.throw("No items found in Credit Note.")
            
                for item in doc.items:
            
                    item_doc = frappe.get_doc("Item", item.item_code)
                    selection_raw = item_doc.get("custom_sage_selection_id")
            
                    if not selection_raw:
                        frappe.throw(
                            f"Sage Selection ID missing for item: {item.item_code}. "
                            f"Please sync inventory first."
                        )
            
                    try:
                        selection_id = int(float(str(selection_raw).strip()))
                    except:
                        frappe.throw(f"Sage Selection ID must be numeric for item: {item.item_code}")
            
                    tax_type_id = int(item_doc.get("tax_typeid_sales") or item_doc.get("custom_sage_tax_type_id") or 0)
            
                    item_exclusive = abs(float(item.net_amount or item.amount or 0))
                    item_rate_excl = abs(float(item.net_rate or item.rate or 0))
                    item_rate_incl = abs(float(item.rate or 0))
                    item_qty = abs(float(item.qty or 0))
                    item_tax = round(item_exclusive * (tax_rate / 100), 2)
                    item_total = round(item_exclusive + item_tax, 2)
            
                    lines.append({
                        "selectionId": selection_id,
                        "id": 0,
                        "lineType": 0,
                        "description": item.description or item.item_name or "",
                        "unit": item.uom or "",
                        "comments": "",
                        "quantity": item_qty,
                        "unitPriceExclusive": item_rate_excl,
                        "unitPriceInclusive": item_rate_incl,
                        "unitCost": abs(float(item.valuation_rate or item.net_rate or item.rate or 0)),
                        "exclusive": item_exclusive,
                        "discount": abs(float(item.discount_amount or 0)),
                        "discountPercentage": abs(float(item.discount_percentage or 0)),
                        "tax": item_tax,
                        "total": item_total,
                        "taxPercentage": round(tax_rate / 100, 4),
                        "taxTypeId": tax_type_id
                    })
            
                # 8. Build Payload
                
                sales_rep_id = 0
                
                # Database se fetch karo
                sales_team = frappe.db.get_all(
                    "Sales Team",
                    filters={"parenttype": "Sales Invoice", "parent": return_against},
                    fields=["sales_person"],
                    limit=1
                )
                
                if sales_team:
                    sp_name = sales_team[0].sales_person
                    rep_doc = frappe.get_doc("Sales Person", sp_name)
                    sales_rep_id = int(rep_doc.get("custom_sage_rep_id") or 0)
                
                # Credit Note se bhi try karo
                if not sales_rep_id:
                    sales_team_cn = frappe.db.get_all(
                        "Sales Team",
                        filters={"parenttype": "Sales Invoice", "parent": doc.name},
                        fields=["sales_person"],
                        limit=1
                    )
                    if sales_team_cn:
                        sp_name = sales_team_cn[0].sales_person
                        rep_doc = frappe.get_doc("Sales Person", sp_name)
                        sales_rep_id = int(rep_doc.get("custom_sage_rep_id") or 0)
                
                # Agar abhi bhi 0 hai toh default use karo
                if not sales_rep_id:
                    sales_rep_id = 740886  # Fisokuhle Radebe - default
                payload = {
                    "credentials": {
                        "loginName": loginName,
                        "loginPwd": loginPwd,
                        "useOAuth": bool(company.use_oauth),
                        "sessionToken": session_token,
                        "provider": provider
                    },
                    "customerReturn": {
                        "date": frappe.utils.formatdate(doc.posting_date, "yyyy-MM-dd") + "T00:00:00",
                        "inclusive": False,
                        "discountPercentage": abs(float(doc.discount_amount or 0)),
                        "taxReference": doc.tax_id or "",
                        "reference": doc.name or "",
                        "message": doc.remarks or "",
                        "discount": abs(float(doc.discount_amount or 0)),
                        "exclusive": abs(float(doc.net_total or 0)),
                        "tax": abs(float(doc.total_taxes_and_charges or 0)),
                        "rounding": abs(float(doc.rounding_adjustment or 0)),
                        "total": abs(float(doc.grand_total or 0)),
                        "amountDue": abs(float(doc.outstanding_amount or doc.grand_total or 0)),
                        "customer": {
                            "id": customer_id
                        },
                        "salesRepresentative": {
                            "id": sales_rep_id
                        },
                        "fromDocument": from_document_number,
                        "fromDocumentId": from_document_id,
                        "fromDocumentTypeId": 1,
                        "lines": lines
                    }
                }
            
                # 9. Send Request
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
                message=f"Credit Note: {doc.name}\nCustomer: {doc.customer}\nError: {str(e)}\nPayload: {str(payload)}",
                title="Sage Customer Return Sync Error"
            )
            try:
                doc.db_set("custom_sage_sync_status", "Failed")
            except Exception:
                pass
            frappe.throw(f"Sage Sync Failed: {str(e)}")