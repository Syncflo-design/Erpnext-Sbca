import frappe
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")
from erpnext_sbca.API.helper_function import (
    is_sync_enabled,
    safe_strip,
    chunks,
    ensure_party_group,
)


def _default_supplier_group():
    """First leaf Supplier Group on this site -- the fallback when a Sage
    supplier carries no category, or its category cannot be created.

    Mirrors customer.py's _default_customer_group(): a leaf must be used
    because ERPNext rejects assigning a Supplier to a group-tree node.
    """
    return (
        frappe.db.get_value(
            "Supplier Group", {"is_group": 0}, "name", order_by="creation asc"
        )
        or "All Supplier Groups"
    )


def get_supplier_categories_from_sage():
    """Mirror Sage's Supplier Categories into ERPNext as leaf Supplier Groups.

    Gated by the `sync_supplier_categories` toggle. Runs ahead of
    get_supplier_from_sage in the scheduler so the groups exist before the
    supplier pull assigns parties into them (the supplier pull also creates
    them lazily via ensure_party_group, so the ordering is a nicety).

    Pharoh endpoint: POST /api/SuppliersSync/get-supplier-categories-for-erpnext
    Response: a bare JSON array of {description, id, modified, created}. Only
    `description` (the category name) is used -- each becomes a leaf Supplier
    Group under "All Supplier Groups". The Sage `id` is intentionally not
    stored: supplier records carry the category by name, so the name is the
    join key.
    """
    if not is_sync_enabled("sync_supplier_categories"):
        return

    settings = frappe.get_doc("Erpnext Sbca Settings")
    company_settings = frappe.db.get_all(
        "Company Sage Integration",
        filters={"parent": settings.name},
        fields=["name"],
    )

    for company in company_settings:
        try:
            company = frappe.get_doc("Company Sage Integration", company.name)
            apikey = company.get_password("api_key")
            payload = {
                "loginName": company.username,
                "loginPwd": company.get_password("password"),
                "useOAuth": bool(company.use_oauth),
                "sessionToken": company.get_password("session_id"),
                "provider": company.get_password("provider"),
            }
            endpoint_url = (
                f"{url}/api/SuppliersSync/get-supplier-categories-for-erpnext"
                f"?apikey={apikey}"
            )

            categories = make_post_request(endpoint_url, json=payload)
            if not isinstance(categories, list):
                frappe.log_error(
                    title=(
                        f"Sage Supplier Category Sync: unexpected response "
                        f"for {company.company}"
                    )[:140],
                    message=(
                        f"Expected a JSON array, got "
                        f"{type(categories).__name__}: {categories}"
                    ),
                )
                continue

            ensured = 0
            for cat in categories:
                if isinstance(cat, dict) and ensure_party_group(
                    "Supplier Group", cat.get("description")
                ):
                    ensured += 1
            frappe.db.commit()
            frappe.logger("sbca").info(
                f"Sage Supplier Category Sync {company.company}: "
                f"{len(categories)} categories returned, {ensured} groups ensured."
            )

        except Exception as e:
            frappe.log_error(
                title=(
                    f"Sage Supplier Category Sync Fatal Error for "
                    f"{company.company}"
                )[:140],
                message=str(e),
            )


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
                        # The `name` field on the Sage payload is the Sage
                        # Supplier ID — purchase_invoice.py reads it via
                        # custom_sage_supplier_id to identify the supplier
                        # on the Sage side. Write it on every upsert.
                        sage_supplier_id = safe_strip(sup_data.get("name")) or ""

                        supplier_filter = {"supplier_name": sup_name}

                        if frappe.db.exists("Supplier", supplier_filter):
                            # Update existing supplier
                            sup_doc = frappe.get_doc("Supplier", supplier_filter)
                            sup_doc.supplier_group = (
                                ensure_party_group(
                                    "Supplier Group",
                                    sup_data.get("supplierGroup"),
                                )
                                or _default_supplier_group()
                            )
                            sup_doc.supplier_type = safe_strip(sup_data.get("supplierType")) or "Company"
                            sup_doc.tax_id = safe_strip(sup_data.get("taxId")) or ""
                            sup_doc.email_id = safe_strip(sup_data.get("emailId")) or ""
                            sup_doc.mobile_no = safe_strip(sup_data.get("mobileNo")) or ""
                            # sup_doc.phone = safe_strip(sup_data.get("phone")) or ""
                            # sup_doc.fax = safe_strip(sup_data.get("fax")) or ""
                            sup_doc.website = safe_strip(sup_data.get("website")) or ""
                            # sup_doc.credit_limit = sup_data.get("creditLimit", 0) or 0
                            sup_doc.supplier_primary_address = None  # reset so we can re-link if address exists
                            if sage_supplier_id:
                                sup_doc.custom_sage_supplier_id = sage_supplier_id
                            sup_doc.save(ignore_permissions=True)
                            updated_suppliers.append(sup_name)
                        else:
                            # Create new supplier
                            sup_doc = frappe.get_doc({
                                "doctype": "Supplier",
                                "supplier_name": sup_name,
                                "supplier_group": (
                                    ensure_party_group(
                                        "Supplier Group",
                                        sup_data.get("supplierGroup"),
                                    )
                                    or _default_supplier_group()
                                ),
                                "supplier_type": safe_strip(sup_data.get("supplierType")) or "Company",
                                "tax_id": safe_strip(sup_data.get("taxId")) or "",
                                "email_id": safe_strip(sup_data.get("emailId")) or "",
                                "mobile_no": safe_strip(sup_data.get("mobileNo")) or "",
                                # "phone": safe_strip(sup_data.get("phone")) or "",
                                # "fax": safe_strip(sup_data.get("fax")) or "",
                                "website": safe_strip(sup_data.get("website")) or "",
                                # "credit_limit": sup_data.get("creditLimit", 0) or 0,
                                # "company": sage.company,
                                "naming_series": "SUP-.YYYY.-",
                                "custom_sage_supplier_id": sage_supplier_id,
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
