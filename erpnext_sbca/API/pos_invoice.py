
import frappe
import json 
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled

payload = {}
def convert_timestamp(ts):
    return frappe.utils.get_datetime(ts).isoformat()

def group_items(items, doc):
    grouped = {}
    if items:
        for item in items:
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
            if not item_doc.get('custom_category'):
                frappe.throw(f"Set Cartegory for {item.item_name}")
            company = item_doc.get('custom_category')
            if company not in grouped:
                grouped[company] = []
            tax_type_id = int(item_doc.get("tax_typeid_sales") or item_doc.get("custom_sage_tax_type_id") or 0)
            item_tax = 0
            tax_details = doc.item_wise_tax_details
            if tax_details != []:
                for tax in tax_details:
                    if item.name == tax.get("item_row"):
                        item_tax = tax.get("amount")
            grouped[company].append({
            "selectionId": selection_id,
            "id": 0,
            "lineType": 0,
            "description":frappe.utils.strip_html(item.description),
            "unit": item.uom or "",
            "comments": "",
            "quantity": item.qty,
            "unitPriceExclusive": item.net_rate,
            "unitPriceInclusive": item.amount,
            "unitCost": item.rate,
            "exclusive": item.net_amount,
            "discount": item.discount_amount or 0,
            "tax": item_tax,
            "total": item.base_amount,
            "taxPercentage": 0,
            "discountPercentage": 0,
            "taxTypeId": tax_type_id if item_tax > 0 else 0,
            "analysisCategoryId1": 0,
            "analysisCategoryId2": 0,
            "analysisCategoryId3": 0
        })
    return grouped

def post_pos_invoice(doc,method):
    if not is_sync_enabled("push_pos_invoice_on_submit"):
        return
    try:
        if doc.is_return != 1 and doc.is_created_using_pos == 1:
            items = group_items(doc.items, doc)
            for key in items.keys():
                if frappe.db.exists("Company Sage Integration", {"company":key}):
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
                        url = f"{url}//api/TaxInvoice/post-taxinvoice-to-sage?apikey={apikey}"
                        invoice_discount_amount = sum( item.get('discount', 0) for item in items[key])
                        invoice_grand_total = sum( item.get('total', 0) for item in items[key])
                        invoice_net_total  = sum( item.get('exclusive', 0) for item in items[key])
                        invoice_tax_amount = sum( item.get('tax', 0) for item in items[key])
                        payload = {
                            "credentials": {
                                "loginName": loginName,
                                "loginPwd": loginPwd,
                                "useOAuth": True,
                                "sessionToken": session_token,
                                "provider": provider
                            },
                            "invoice": {
                            "date": convert_timestamp(doc.creation),
                            "inclusive": True,
                            "discountPercentage": 0,
                            "taxReference": "",
                            "customerName": company.get("sage_pos_customer_name"),
                            "customerId":company.get("sage_pos_customer_id"),
                            "dueDate": convert_timestamp(doc.creation),
                            "status": "",
                            "customer": {
                            "id": company.get("sage_pos_customer_id"),
                            "salesRepresentativeId": 0,
                            "salesRepresentative": {
                            "firstName": "",
                            "lastName": "",
                            "name": "",
                            "category": "",
                            "active": True,
                            "email": "",
                            "mobile": "",
                            "telephone": "",
                            "created":  convert_timestamp(doc.creation),
                            "modified":  convert_timestamp(doc.creation),
                            },
                            "taxReference": "",
                            "category": {
                            "description": "",
                                "id": 0,
                                "modified":  convert_timestamp(doc.creation),
                                "created":  convert_timestamp(doc.creation),
                            },
                            "name": doc.name,
                            "contactName": "",
                            "telephone": "",
                            "fax": "",
                            "mobile": "",
                            "email": "",
                            "active": True,
                            "creditLimit": 0,
                            "postalAddress01": "",
                            "postalAddress02": "",
                            "postalAddress03": "",
                            "postalAddress04": "",
                            "postalAddress05": "",
                            "deliveryAddress01": "",
                            "deliveryAddress02": "",
                            "deliveryAddress03": "",
                            "deliveryAddress04": "",
                            "deliveryAddress05": "",
                            "defaultPriceListId": 0,
                            "defaultDiscountPercentage": 0,
                            "defaultTaxTypeId": 0,
                            "taxType": {
                                "id": 0,
                                "name": "",
                                "percentage": 0,
                                "active": True,
                                "companyId": 0
                            }
                            },
                            "salesRepresentative": {
                            "id": 0,
                            "firstName": "",
                            "lastName": "",
                            "name": "",
                            "category": "",
                            "active": True,
                            "email": "",
                            "mobile": "",
                            "telephone": "",
                            "created":  convert_timestamp(doc.creation),
                            },
                            "reference": "",
                            "message": "",
                            "discount": invoice_discount_amount,
                            "exclusive": invoice_net_total,
                            "tax": invoice_tax_amount,
                            "rounding": 0,
                            "total": invoice_grand_total,
                            "amountDue": 0,
                            "lines": items[key],
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
                            "externalReference": "",
                            "fromDocument": "",
                            "fromDocumentId": 0,
                            "fromDocumentTypeId": 0
                        }}
                        
                        sage_response_text = "No response captured"
                        payload = json.dumps(payload)
                        try:
                            response = make_post_request(
                                url,
                                data= payload,
                                headers={
                                "Content-Type": "application/json"
                            })
                            if response and response.get("success"):
                                frappe.msgprint(
                                    f"✅ Sage Sync Successful!\n"
                                    f"Sage Order ID: {response.get('sageOrderId')}\n"
                                    f"Document Number: {response.get('documentNumber')}"
                                )
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
                            frappe.log_error(str(http_err), "POS Sales Invoice API Error")
                            raise
    except Exception as e:
        frappe.log_error(
            message=f"POS Sales Invoice:  {doc.name}\nError: {str(e)}\nPayload: {str(payload)}",
            title="Sage POS Sales Invoice Sync Error"
        )
        try:
            doc.db_set("custom_sage_sync_status", "Failed")
        except Exception:
            pass
        frappe.throw(f"Sage Sync Failed: {str(e)}")
    finally:
        doc.reload()















