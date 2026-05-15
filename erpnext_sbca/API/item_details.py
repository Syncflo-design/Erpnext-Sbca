import frappe
from erpnext_sbca.API.helper_function import as_int, is_sync_enabled, safe_strip, chunks, resolve_is_stock_item
from frappe.integrations.utils import (
	make_post_request,
)
url = frappe.db.get_single_value("Erpnext Sbca Settings", "url")


# ---------------------------------------------------------------------------
# Custom field plumbing — Sage Price List ID lives on Price List so the
# customer pull (and any future caller) can resolve a Sage `id` to the
# corresponding ERPNext Price List name. Populated by get_price_list_from_sage.
# ---------------------------------------------------------------------------

def _ensure_sage_price_list_id_field():
    """Idempotently add `custom_sage_price_list_id` to Price List.

    Same pattern as account.py's `_ensure_sage_managed_field()`. Safe to
    call on every sync tick — exits immediately if the field exists.
    """
    if frappe.db.exists(
        "Custom Field",
        {"dt": "Price List", "fieldname": "custom_sage_price_list_id"},
    ):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Price List",
                "fieldname": "custom_sage_price_list_id",
                "label": "Sage Price List ID",
                "fieldtype": "Data",
                "read_only": 1,
                "description": (
                    "Set by the Sage Price List sync. Lets the customer "
                    "pull and the additional-prices pull resolve Sage's "
                    "internal pricelistID to the matching ERPNext Price "
                    "List record."
                ),
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            title="Sage Sync: could not create custom_sage_price_list_id field",
            message=str(e),
        )


def get_item_inventory_qty_on_hand_from_sage():
    if not is_sync_enabled("sync_stock_on_hand"):
        return
    settings = frappe.get_doc("Erpnext Sbca Settings")
    company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
    for company in company_settings:
        company = frappe.get_doc("Company Sage Integration", company.name)
        # Once a Company has cut over (stock imported into ERPNext + Sage qty
        # tracking disabled), ERPNext owns the stock. Skip it here so Sage's
        # qty-on-hand pull can never overwrite ERPNext's authoritative levels.
        if company.get("stock_import_complete"):
            continue
        apikey = company.get_password("api_key")
        loginName = company.username
        loginPwd = company.get_password("password")
        provider = company.get_password("provider")
        session_token = company.get_password("session_id")
        lastDate = "1970-01-01"
        inventory_url = f"{url}/api/InventorySync/get-inventory-qtyonhand-for-erpnext?apikey={apikey}&lastDate={lastDate}"

        payload = {
            "loginName": loginName,
            "loginPwd": loginPwd,
            "useOAuth": bool(company.use_oauth),
            "sessionToken": session_token,
            "provider": provider
        }

        def safe_float(val):
            try:
                return float(val)
            except (TypeError, ValueError):
                return 0.0

        try:
            # Fetch inventory from Sage
            inventory = make_post_request(inventory_url, json=payload)

            updated_items = []
            skipped_items = []

            batch_size = 50  # process 50 items at a time

            for batch in chunks(inventory, batch_size):
                for item_data in batch:
                    item_code = item_data.get("code")
                    if not item_code:
                        skipped_items.append(None)
                        continue

                    try:
                        if frappe.db.exists("Item", {"item_code": item_code}):
                            item_doc = frappe.get_doc("Item", {"item_code": item_code})

                            # Update main fields
                            item_doc.valuation_rate = safe_float(item_data.get("averageCost"))
                            item_doc.standard_rate = safe_float(item_data.get("priceExclusive"))

                            # Update informational fields
                            item_doc.last_purchase_rate = safe_float(item_data.get("lastCost"))
                            item_doc.custom_quantity_on_hand = safe_float(item_data.get("quantityOnHand"))

                            item_doc.save(ignore_permissions=True)
                            updated_items.append(item_code)
                        else:
                            skipped_items.append(item_code)

                    except Exception as e:
                        # Truncate error title to 140 chars
                        title = f"Error processing Item {item_code}"[:140]
                        frappe.log_error(message=str(e), title=title)
                        skipped_items.append(item_code)

                # Commit after each batch
                frappe.db.commit()

        except Exception as e:
            frappe.log_error(message=str(e), title="Sage Inventory Sync Fatal Error"[:140])

def get_addition_prices_from_sage():
    if not is_sync_enabled("sync_additional_prices"):
        return
    # The dynamic pricelist_ids query below filters on
    # custom_sage_price_list_id. Make sure the field exists on a fresh
    # site even if the user disabled sync_price_lists — otherwise the
    # filter would crash with an unknown-column error.
    _ensure_sage_price_list_id_field()
    settings = frappe.get_doc("Erpnext Sbca Settings")
    company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
    for company in company_settings:
        company = frappe.get_doc("Company Sage Integration", company.name)
        apikey = company.get_password("api_key")
        loginName = company.username
        loginPwd = company.get_password("password")
        provider = company.get_password("provider")
        session_token = company.get_password("session_id")
        # Fetch all existing Price Lists from DB
        existing_price_lists = {pl.price_list_name: pl.name for pl in frappe.get_all("Price List", fields=["name", "price_list_name"])}

        # Fetch all existing Items
        existing_items = {item.item_code: item for item in frappe.get_all("Item", fields=["name", "item_code", "item_name", "description"])}

        # Fetch existing Item Prices
        existing_item_prices = set(
            (ip.item_code, ip.price_list)
            for ip in frappe.get_all("Item Price", fields=["item_code", "price_list"])
        )

        created = []
        updated = []
        skipped = []
        errors = []

        # Drive the list of Sage pricelistIDs from the Price List records
        # we've pulled (each Price List carries its Sage ID in
        # custom_sage_price_list_id). Falls back to the historical
        # hardcoded pair on a fresh install where get_price_list_from_sage
        # hasn't run yet — that fallback evaporates after the first
        # successful price-list pull.
        pricelist_ids = [
            pl.custom_sage_price_list_id
            for pl in frappe.get_all(
                "Price List",
                filters={"custom_sage_price_list_id": ["is", "set"]},
                fields=["custom_sage_price_list_id"],
            )
            if pl.custom_sage_price_list_id
        ]
        if not pricelist_ids:
            pricelist_ids = ["3796", "3795"]

        login_payload = {
            "loginName": loginName,
            "loginPwd": loginPwd,
            "useOAuth": bool(company.use_oauth),
            "sessionToken": session_token,
            "provider": provider
        }

        for pl_id in pricelist_ids:
            item_prices_url = f"{url}/api/AdditionalItemPricesSync/get-additional-prices-for-erpnext?apikey={apikey}&pricelistID={pl_id}"

            # ✅ Add timeout and retry
            try:
                item_prices_response = make_post_request(item_prices_url, json=login_payload)  # timeout in seconds
            except Exception as e:
                errors.append(f"Failed fetching item prices for Pricelist {pl_id}: {e}")
                continue

            for ip in item_prices_response:
                item_code = ip.get("itemCode")
                price_list_name = ip.get("priceListName")
                rate = ip.get("priceListRate", 0)

                if not item_code or not price_list_name:
                    skipped.append(f"Unknown item or price list in {pl_id}")
                    continue

                price_list_docname = existing_price_lists.get(price_list_name)
                if not price_list_docname:
                    skipped.append(item_code)
                    continue

                item_info = existing_items.get(item_code)
                if not item_info:
                    skipped.append(item_code)
                    continue

                # Update existing Item Price
                if (item_code, price_list_docname) in existing_item_prices:
                    try:
                        doc = frappe.get_doc("Item Price", {"item_code": item_code, "price_list": price_list_docname})
                        doc.price_list_rate = rate
                        doc.save(ignore_permissions=True)
                        updated.append(item_code)
                    except Exception as e:
                        errors.append(f"Error updating Item Price for {item_code}: {e}")
                        skipped.append(item_code)
                    continue

                # Create new Item Price
                try:
                    new_item_price = frappe.get_doc({
                        "doctype": "Item Price",
                        "price_list": price_list_docname,
                        "item_code": item_code,
                        "price_list_rate": rate,
                        "uom": "Nos",
                        "selling": 1,
                        "buying": 1,
                        "currency": "ZAR",
                        "item_name": item_info.item_name,
                        "item_description": item_info.description
                    })
                    new_item_price.insert(ignore_permissions=True)
                    created.append(item_code)
                except Exception as e:
                    errors.append(f"Error creating Item Price for {item_code}: {e}")
                    skipped.append(item_code)



def get_price_list_from_sage():
    if not is_sync_enabled("sync_price_lists"):
        return
    _ensure_sage_price_list_id_field()
    settings = frappe.get_doc("Erpnext Sbca Settings")
    company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
    for company in company_settings:
        company = frappe.get_doc("Company Sage Integration", company.name)
        apikey = company.get_password("api_key")
        loginName = company.username
        loginPwd = company.get_password("password")
        provider = company.get_password("provider")
        session_token = company.get_password("session_id")
        pricelist_url = f"{url}/api/AdditionalPriceListSync/get-pricelists-for-erpnext?apikey={apikey}"

        payload = {
            "loginName": loginName,
            "loginPwd": loginPwd,
            "useOAuth": bool(company.use_oauth),
            "sessionToken": session_token,
            "provider": provider
        }



        created = []
        updated = []
        skipped = []
        errors = []

        # Make external request with timeout
        try:
            pricelists = make_post_request(pricelist_url, json=payload)  # add timeout
        except Exception as e:
            frappe.response["message"] = {"created": [], "updated": [], "skipped": [], "errors": [str(e)]}
            frappe.throw(f"Error fetching price lists: {e}")

        # Get all existing price lists at once
        existing_price_lists = {
            pl.price_list_name: pl.name
            for pl in frappe.get_all("Price List", fields=["name", "price_list_name"])
        }

        for pl in pricelists:
            pl_name = safe_strip(pl.get("name"))
            pl_desc = safe_strip(pl.get("description"))
            pl_default = as_int(pl.get("isDefault"))
            pl_enabled = as_int(pl.get("enabled"))
            # Sage's `id` is the pricelistID used by the additional-prices
            # endpoint AND referenced by Customer.default_price_list_id.
            # Stored verbatim as a string so it round-trips cleanly.
            pl_sage_id = str(pl.get("id")) if pl.get("id") is not None else ""

            if pl_name in existing_price_lists:
                try:
                    doc = frappe.get_doc("Price List", existing_price_lists[pl_name])
                    doc.custom_description = pl_desc
                    doc.custom_is_default = pl_default
                    doc.enabled = pl_enabled
                    if pl_sage_id:
                        doc.custom_sage_price_list_id = pl_sage_id
                    doc.save(ignore_permissions=True)
                    updated.append(pl_name)
                except Exception as e:
                    errors.append(f"Error updating {pl_name}: {e}")
                    skipped.append(pl_name)
                continue

            try:
                new_doc = frappe.get_doc({
                    "doctype": "Price List",
                    "price_list_name": pl_name,
                    "custom_description": pl_desc,
                    "custom_is_default": pl_default,
                    "enabled": pl_enabled,
                    "selling": 1,
                    "buying": 1,
                    "currency": "ZAR",
                    "custom_sage_price_list_id": pl_sage_id,
                })
                new_doc.insert(ignore_permissions=True)
                created.append(pl_name)
            except Exception as e:
                errors.append(f"Error creating {pl_name}: {e}")
                skipped.append(pl_name)

        frappe.response["message"] = {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors
        }


def update_item_job():
    if not is_sync_enabled("push_item_updates_scheduled"):
        return
    cron = frappe.get_doc("Scheduled Job Type","update_item_add_info_cron")
    cron.reload()
    cron.stopped = 0
    cron.save()
    server_script = frappe.get_doc("Server Script", "update-item-add-info")
    server_script.reload()
    server_script.disabled = 0
    server_script.save()


def update_prices():
    if not is_sync_enabled("push_item_prices_scheduled"):
        return
    settings = frappe.get_doc("Erpnext Sbca Settings")
    company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
    for company in company_settings:
        company = frappe.get_doc("Company Sage Integration", company.name)
        apikey = company.get_password("api_key")
        loginName = company.username
        loginPwd = company.get_password("password")
        provider = company.get_password("provider")
        session_token = company.get_password("session_id")
        lastDate = "1970-01-01"
        inventory_url = f"{url}/api/InventorySync/get-inventory-for-erpnext?apikey={apikey}&lastDate={lastDate}"

        payload = {
            "loginName": loginName,
            "loginPwd": loginPwd,
            "useOAuth": bool(company.use_oauth),
            "sessionToken": session_token,
            "provider": provider
        }

        try:
            # Fetch items from Sage
            inventory_items = make_post_request(inventory_url, json=payload)

            updated_items = []
            created_items = []
            skipped_items = []

            batch_size = 50  # process 50 items at a time

            for batch in chunks(inventory_items, batch_size):
                for item_data in batch:
                    item_code = safe_strip(item_data.get("item_code"))
                    if not item_code:
                        skipped_items.append(None)
                        continue

                    try:
                        if frappe.db.exists("Item", {"item_code": item_code}):
                            # Update existing item
                            item_doc = frappe.get_doc("Item", {"item_code": item_code})
                            item_doc.valuation_rate = item_data.get("valuation_rate") or 0
                            item_doc.standard_rate = item_data.get("standard_rate") or 0
                            item_doc.last_purchase_rate = item_data.get("last_purchase_rate") or 0
                            item_doc.save(ignore_permissions=True)
                            updated_items.append(item_code)
                        else:
                            # Create new item — is_stock_item from Sage's
                            # physical/service flag (set once, on create).
                            item_doc = frappe.get_doc({
                                "doctype": "Item",
                                "item_code": item_code,
                                "item_name": safe_strip(item_data.get("item_name")) or item_code,
                                "stock_uom": "Nos",
                                "item_group":"All Item Groups",
                                "custom_sub_category_size":"Small",
                                "is_stock_item": resolve_is_stock_item(item_data),
                                "is_sales_item": 1,
                                "is_purchase_item": 1,
                                "valuation_rate": item_data.get("valuation_rate") or 0,
                                "standard_rate": item_data.get("standard_rate") or 0,
                                "last_purchase_rate": item_data.get("last_purchase_rate") or 0,
                                "description": safe_strip(item_data.get("description")) or ""
                            })
                            item_doc.insert(ignore_permissions=True)
                            created_items.append(item_code)

                    except Exception as e:
                        frappe.log_error(f"Error processing Item {item_code}: {str(e)}", "Sage Sync Error")
                        skipped_items.append(item_code)

                # Commit after each batch
                frappe.db.commit()

            frappe.logger().info(
                f"Sage Inventory Sync Done. "
                f"Updated: {len(updated_items)}, Created: {len(created_items)}, Skipped: {len(skipped_items)}"
            )

        except Exception as e:
            frappe.log_error(f"Sage Inventory Sync Failed: {str(e)}", "Sage Sync Error")


def get_categories_from_sage():
    if not is_sync_enabled("sync_item_categories"):
        return
    settings = frappe.get_doc("Erpnext Sbca Settings")
    company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
    for company in company_settings:
        company = frappe.get_doc("Company Sage Integration", company.name)
        apikey = company.get_password("api_key")
        loginName = company.username
        loginPwd = company.get_password("password")
        provider = company.get_password("provider")
        session_token = company.get_password("session_id")
        lastDate = "1970-01-01"
        endpoint_url = f"{url}/api/ItemCategorySync/get-categories-for-erpnext?apikey={apikey}&lastDate={lastDate}"

        payload = {
            "loginName": loginName,
            "loginPwd": loginPwd,
            "useOAuth": bool(company.use_oauth),
            "sessionToken": session_token,
            "provider": provider
        }

        # Send POST request to Pharoh API
        items = make_post_request(endpoint_url, json=payload)

        for item_group_data in items:
            # Get item group name
            item_group_name = item_group_data.get("item_group_mame")
            if not item_group_name:
                continue

            # Check if item group exists, create if it doesn't
            if frappe.db.exists("Item Group", item_group_name):
                group_doc = frappe.get_doc("Item Group", item_group_name)
            else:
                group_doc = frappe.new_doc("Item Group")
                group_doc.item_group_name = item_group_name

            # Update item group fields
            group_doc.parent_item_group = item_group_data.get("parent_item_group")
            group_doc.is_group = item_group_data.get("is_group", 0)

            group_doc.save(ignore_permissions=True)

        # Final commit only once after all item groups are processed
        frappe.db.commit()


def get_inventory_from_sage():
    if not is_sync_enabled("sync_items"):
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
            skipQty = 0
            has_more = True

            while has_more:
                endpoint_url = f"{url}/api/InventorySync/get-inventory-for-erpnext?apikey={apikey}&lastDate={lastDate}&skipQty={skipQty}"
                payload = {"loginName": loginName, "loginPwd": loginPwd, "useOAuth": bool(company.use_oauth), "sessionToken": session_token, "provider": provider}

                response = make_post_request(endpoint_url, json=payload)

                items = response.get("items") or []
                total = response.get("totalResults", 0)
                returned = response.get("returnedResults", 0)

                # stop if no items
                if not items:
                    break

                for item_data in items:
                    try:
                        item_code = item_data.get("item_code")
                        if not item_code:
                            continue

                        uom = item_data.get("stock_uom")
                        if not uom:
                            continue

                        if not frappe.db.exists("UOM", uom):
                            uom_doc = frappe.new_doc("UOM")
                            uom_doc.uom_name = uom
                            uom_doc.enabled = 1
                            uom_doc.insert(ignore_permissions=True)

                        item_group = item_data.get("item_group") or "All Item Groups"
                        if not frappe.db.exists("Item Group", item_group):
                            if not frappe.db.exists("Item Group", "All Item Groups"):
                                item_group = "Products"
                            group_doc = frappe.new_doc("Item Group")
                            group_doc.item_group_name = item_group
                            group_doc.parent_item_group = "All Item Groups"
                            group_doc.is_group = 0
                            group_doc.insert(ignore_permissions=True)

                        is_new_item = not frappe.db.exists("Item", item_code)
                        if is_new_item:
                            item_doc = frappe.new_doc("Item")
                            item_doc.item_code = item_code
                        else:
                            item_doc = frappe.get_doc("Item", item_code)

                        item_doc.item_name = item_data.get("item_name") or f"{item_code} - Item"
                        item_doc.description = item_data.get("description") or item_doc.item_name
                        item_doc.item_group = item_group
                        # is_stock_item is set ONCE, on create, from Sage's
                        # physical/service flag. After the item exists, ERPNext
                        # owns the stock-tracking decision and the sync never
                        # touches it again — otherwise a stock cutover (which
                        # flips Sage's flag to service) would silently disable
                        # ERPNext stock tracking on the next sync tick.
                        if is_new_item:
                            item_doc.is_stock_item = resolve_is_stock_item(item_data)
                            item_doc.stock_uom = uom
                        item_doc.standard_rate = item_data.get("standard_rate", 0.0)
                        item_doc.custom_retail_price_incl_vat=item_data.get("standard_rate_incl", 0.0)
                        item_doc.custom_item_barcode=item_code
                        item_doc.valuation_rate = item_data.get("valuation_rate", 0.0)
                        item_doc.is_sales_item = item_data.get("is_sales_item", 0)
                        item_doc.is_purchase_item = item_data.get("is_purchase_item", 0)
                        item_doc.disabled = item_data.get("disabled", 0)
                        
                        # Save Sage Selection ID if available in API response
                        sage_item_id = (item_data.get("SelectionID") or 
                            item_data.get("selectionId") or 
                            item_data.get("custom_sage_selection_id") or 
                            item_data.get("id"))
                        if sage_item_id:
                            item_doc.custom_sage_selection_id = str(sage_item_id)
                        
                        tax_type_id = item_data.get("tax_typeid_sales")
                        if tax_type_id:
                            item_doc.custom_sage_tax_type_id = tax_type_id
                        
                        item_doc.custom_size = None

                        item_doc.save(ignore_permissions=True)

                    except Exception as inner:
                        frappe.log_error(message=str(inner), title=f"Error processing Item {item_data.get('item_code')}")

                frappe.db.commit()

                skipQty = skipQty + returned
                has_more = skipQty < total

        except Exception as outer:
            frappe.log_error(message=str(outer), title=f"Sage Item Sync Fatal Error for {company.company}")
