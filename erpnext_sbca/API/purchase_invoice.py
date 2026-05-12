import frappe
import json
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled


def convert_timestamp(ts):
    return ts.isoformat()

def post_purchase_invoice(doc, method):
    """Wrapper: enqueue the push so we don't block the Purchase Invoice submit transaction."""
    if not is_sync_enabled("push_purchase_invoice_on_submit"):
        return
    frappe.enqueue(
        "erpnext_sbca.API.purchase_invoice._post_purchase_invoice_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doc_name=doc.name,
    )


def _post_purchase_invoice_worker(doc_name):
    doc = frappe.get_doc("Purchase Invoice", doc_name)
    try:
        if doc.is_return == 0:
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

                endpoint_url = f"{url}/api/SupplierInvoice/post-supplierinvoice-to-sage?apikey={apikey}"

                supplier_doc = frappe.get_doc("Supplier", doc.supplier)
                sage_supplier_id = supplier_doc.get("custom_sage_supplier_id")
                selection_id = 0
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
                    tax_type_id = int(item_doc.get("tax_typeid_sales") or item_doc.get("custom_sage_tax_type_id") or 0)
                    item_exclusive = float(item.base_amount or 0)
                    item_rate_excl = float(item.net_rate or 0)
                    item_rate_incl = float(item.rate or 0)
                    item_tax = 0
                    tax_details = doc.item_wise_tax_details
                    if tax_details != []:
                        for tax in tax_details:
                            if item.name == tax.get("item_row"):
                                item_tax = tax.get("amount")
                    item_total = item.base_amount
                    lines.append (
                        {
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
                    "exclusive":item_exclusive,
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
                }
                )

                payload = {
                    "credentials": {
                        "loginName": loginName,
                        "loginPwd": loginPwd,
                        "useOAuth": bool(company.use_oauth),
                        "sessionToken": session_token,
                        "provider": provider
                    },
                    "invoice": {
                "id": 0,
                "documentNumber": doc.name or "",
                "date": convert_timestamp(doc.creation),
                "dueDate": convert_timestamp(doc.creation),
                "status": "",
                "supplier": {
                "id": supplier_id,
                "name": doc.name or "",
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
                "textField1": "string",
                "textField2": "string",
                "textField3": "string",
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
                "businessRegistrationNumber": "string",
                "rmcdApprovalNumber": "string",
                "taxStatusVerified": convert_timestamp(doc.creation),
                "currencyId": 0,
                "currencySymbol": "string",
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
                "fromDocument": "",
                "fromDocumentId": 0,
                "fromDocumentTypeId": 0,
                "paid": True,
                "locked": True,
                "hasAdditionalCost": True,
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
            }
                }

                sage_response_text = "No response captured"
                payload = json.dumps(payload)
                try:

                    response = make_post_request(

                        endpoint_url,

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

                    else:

                        error_msg = response.get("errorMessage") or str(response) if response else "Unknown"

                        frappe.throw(f"Sage API Error: {error_msg}")

                except Exception as http_err:
                    frappe.log_error(str(http_err), "SupplierReturn API Error")
                    raise

    except Exception as e:

        frappe.log_error(

            message=f"Purchase Invoice: {doc.name}\nSupplier: {doc.supplier}\nError: {str(e)}\nPayload: {str(payload)}",

            title="Sage Purchase Invoice Sync Error"

        )
        try:
            doc.db_set("custom_sage_sync_status", "Failed")

        except Exception:

            pass

        frappe.throw(f"Sage Sync Failed: {str(e)}")
    finally:
        doc.reload()


def post_purchase_invoice_return(doc, method):
    """Wrapper: enqueue the push so we don't block the Purchase Invoice (return) submit transaction."""
    if not is_sync_enabled("push_purchase_invoice_return_on_submit"):
        return
    frappe.enqueue(
        "erpnext_sbca.API.purchase_invoice._post_purchase_invoice_return_worker",
        queue="default",
        timeout=600,
        enqueue_after_commit=True,
        doc_name=doc.name,
    )


def _post_purchase_invoice_return_worker(doc_name):
    doc = frappe.get_doc("Purchase Invoice", doc_name)
    try:
        if doc.is_return == 1:
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

                endpoint_url = f"{url}/api/SupplierReturn/post-supplierreturn-to-sage?apikey={apikey}"
                supplier_id = 0
                supplier_doc = frappe.get_doc("Supplier", doc.supplier)
                sage_supplier_id = supplier_doc.get("custom_sage_supplier_id") or "0"
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
                        selection_id = int(float(str(selection_raw).strip()))
                    except:
                        frappe.throw(f"Sage Selection ID must be numeric for item: {item.item_code}")
                    
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
                    lines.append (
                        {
                    "selectionId": selection_id,
                    "taxTypeId": tax_type_id,
                    "id": 0,
                    "description": item.description or item.item_name or "",
                    "lineType": 0,
                    "quantity": float(item.qty or 0),
                    "unitPriceExclusive": item_rate_excl,
                    "unit": item.uom or "",
                    "unitPriceInclusive": item_rate_incl,
                    "taxPercentage": 0,
                    "discountPercentage": float(item.discount_percentage or 0),
                    "exclusive": item_exclusive,
                    "discount": float(item.discount_amount or 0),
                    "tax": item_tax,
                    "total": item.base_amount,
                    "comments": "",
                    "analysisCategoryId1": 0,
                    "analysisCategoryId2": 0,
                    "analysisCategoryId3": 0,
                    "trackingCode": "",
                    "currencyId": 0,
                    "unitCost":  float(item.valuation_rate or item.net_rate or item.rate or 0),
                    "uid": ""
                }
                )

                payload = {

                    "credentials": {
                        "loginName": loginName,
                        "loginPwd": loginPwd,
                        "useOAuth": bool(company.use_oauth),
                        "sessionToken": session_token,
                        "provider": provider

                    },
                "return": {
                "fromDocument": doc.return_against,
                "locked": True,
                "trackingCode": "",
                "supplierId": supplier_id,
                "supplierName": doc.supplier_name,
                "supplier": {
                "id": supplier_id,
                "name": doc.supplier,
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
                "modified": convert_timestamp(doc.modified),
                "created": convert_timestamp(doc.creation),
                "statusId": 0,
                "supplier_CurrencyId": 0,
                "supplier_ExchangeRate": 0,
                "id": 0,
                "date": convert_timestamp(doc.creation),
                "inclusive": True,
                "discountPercentage": float(doc.additional_discount_percentage or 0),
                "taxReference": "",
                "documentNumber": doc.name or "",
                "reference": doc.name or "",
                "message": doc.remarks or "",
                "discount": float(doc.discount_amount or 0),
                "exclusive": float(doc.grand_total or 0),
                "tax": float(doc.total_taxes_and_charges or 0),
                "rounding": float(doc.rounding_adjustment or 0),
                "total": float(doc.grand_total or 0),
                "amountDue": float(doc.outstanding_amount or 0),
                "postalAddress01": doc.shipping_address,
                "postalAddress02": doc.shipping_address,
                "postalAddress03": doc.shipping_address,
                "postalAddress04": doc.shipping_address,
                "postalAddress05": doc.shipping_address,
                "deliveryAddress01": doc.shipping_address,
                "deliveryAddress02": doc.shipping_address,
                "deliveryAddress03": doc.shipping_address,
                "deliveryAddress04": doc.shipping_address,
                "deliveryAddress05": doc.shipping_address,
                "printed": True,
                "taxPeriodId": 0,
                "editable": True,
                "hasAttachments": True,
                "hasNotes": True,
                "hasAnticipatedDate": True,
                "hasSpecialCountryTax": True,
                "anticipatedDate": convert_timestamp(doc.creation),
                "externalReference": "",
                "uid": "",
                "lines": lines
            }
                }

                sage_response_text = "No response captured"
                payload = json.dumps(payload)
                try:
                    response = make_post_request(

                        endpoint_url,

                        data= payload,

                        headers={
            "Content-Type": "application/json"
        }

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
                    frappe.log_error(str(http_err), "SupplierReturn API Error")
                    raise

    except Exception as e:

        frappe.log_error(

            message=f"Purchase Invoice Return:  {doc.name}\nCustomer: {doc.supplier}\nError: {str(e)}\nPayload: {str(payload)}",

            title="Sage Purchase Invoice Return Sync Error"

        )
        try:
            doc.db_set("custom_sage_sync_status", "Failed")

        except Exception:

            pass

        frappe.throw(f"Sage Sync Failed: {str(e)}")

    finally:
        doc.reload()



