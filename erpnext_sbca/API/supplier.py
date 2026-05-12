import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import is_sync_enabled

def safe_strip(value):
    return value.strip() if isinstance(value, str) else value

def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def get_supplier_from_sage():
    if not is_sync_enabled("sync_suppliers"):
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
            lastDate = "1970-01-01"

            supplier_url = f"{url}/api/SuppliersSync/get-suppliers-for-erpnext?apikey={apikey}&lastDate={lastDate}"
            payload = {
                "loginName": loginName,
                "loginPwd": loginPwd,
                "useOAuth": bool(company.use_oauth),
                "sessionToken": session_token,
                "provider": provider

            }

            # Fetch suppliers from Sage
            suppliers = make_post_request(supplier_url, json=payload)

            updated_suppliers = []
            created_suppliers = []
            skipped_suppliers = []

            batch_size = 50

            for batch in chunks(suppliers, batch_size):
                for sup_data in batch:
                    sup_name = safe_strip(sup_data.get("supplierName"))
                    if not sup_name:
                        skipped_suppliers.append(None)
                        continue

                    try:
                        supplier_filter = {"supplier_name": sup_name}

                        if frappe.db.exists("Supplier", supplier_filter):
                            # Update existing supplier
                            sup_doc = frappe.get_doc("Supplier", supplier_filter)
                            sup_doc.supplier_group = safe_strip(sup_data.get("supplierGroup")) or "All Supplier Groups"
                            sup_doc.supplier_type = safe_strip(sup_data.get("supplierType")) or "Company"
                            sup_doc.tax_id = safe_strip(sup_data.get("taxId")) or ""
                            sup_doc.email_id = safe_strip(sup_data.get("emailId")) or ""
                            sup_doc.mobile_no = safe_strip(sup_data.get("mobileNo")) or ""
                            # sup_doc.phone = safe_strip(sup_data.get("phone")) or ""
                            # sup_doc.fax = safe_strip(sup_data.get("fax")) or ""
                            sup_doc.website = safe_strip(sup_data.get("website")) or ""
                            # sup_doc.credit_limit = sup_data.get("creditLimit", 0) or 0
                            sup_doc.supplier_primary_address = None  # reset so we can re-link if address exists
                            sup_doc.save(ignore_permissions=True)
                            updated_suppliers.append(sup_name)
                        else:
                            # Create new supplier
                            sup_doc = frappe.get_doc({
                                "doctype": "Supplier",
                                "supplier_name": sup_name,
                                "supplier_group": safe_strip(sup_data.get("supplierGroup")) or "All Supplier Groups",
                                "supplier_type": safe_strip(sup_data.get("supplierType")) or "Company",
                                "tax_id": safe_strip(sup_data.get("taxId")) or "",
                                "email_id": safe_strip(sup_data.get("emailId")) or "",
                                "mobile_no": safe_strip(sup_data.get("mobileNo")) or "",
                                # "phone": safe_strip(sup_data.get("phone")) or "",
                                # "fax": safe_strip(sup_data.get("fax")) or "",
                                "website": safe_strip(sup_data.get("website")) or "",
                                # "credit_limit": sup_data.get("creditLimit", 0) or 0,
                                # "company": sage.company,
                                "naming_series": "SUP-.YYYY.-"
                            })
                            sup_doc.insert(ignore_permissions=True)
                            created_suppliers.append(sup_name)

                            # Create Address if available
                            # if any([sup_data.get("addressLine1"), sup_data.get("city"), sup_data.get("country")]):
                            #     addr_doc = frappe.get_doc({
                            #         "doctype": "Address",
                            #         "address_title": sup_name,
                            #         "address_line1": safe_strip(sup_data.get("addressLine1")) or "",
                            #         "address_line2": safe_strip(sup_data.get("addressLine2")) or "",
                            #         "city": safe_strip(sup_data.get("city")) or "",
                            #         "state": safe_strip(sup_data.get("state")) or "",
                            #         "pincode": safe_strip(sup_data.get("postalCode")) or "",
                            #         "country": safe_strip(sup_data.get("country")) or "",
                            #         "phone": safe_strip(sup_data.get("phone")) or "",
                            #         "fax": safe_strip(sup_data.get("fax")) or "",
                            #         "links": [{
                            #             "link_doctype": "Supplier",
                            #             "link_name": sup_doc.name
                            #         }]
                            #     })
                            #     addr_doc.insert(ignore_permissions=True)

                    except Exception as e:
                        title = f"Error processing Supplier {sup_name}"[:140]
                        frappe.log_error(message=str(e), title=title)
                        skipped_suppliers.append(sup_name)


            summary = f"Company: {company.company} | Updated: {len(updated_suppliers)}, Created: {len(created_suppliers)}, Skipped: {len(skipped_suppliers)}"
            frappe.log_error(message=summary, title=f"Sage Supplier Sync Summary for {company.company}"[:140])

        except Exception as e:
            title = f"Sage Supplier Sync Fatal Error for {company.company}"[:140]
            frappe.log_error(message=str(e), title=title)
