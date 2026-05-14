
def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def get_parent_account(root_type, company_name):
    """Return parent account name based on root_type and company suffix"""
    # Inline strip logic
    def strip_if_str(value):
        return value.strip() if isinstance(value, str) else value
    
    initial = company_name[0].upper() if company_name else "C"
    root_type_lower = strip_if_str(root_type or "").lower()

    if "asset" in root_type_lower:
        return f"Application of Funds (Assets) - {initial}"
    elif "liabilit" in root_type_lower:
        return f"Source of Funds (Liabilities) - {initial}"
    elif "equity" in root_type_lower:
        return f"Equity - {initial}"
    elif "income" in root_type_lower:
        return f"Income - {initial}"
    elif "expense" in root_type_lower:
        return f"Expenses - {initial}"
    else:
        return f"Application of Funds (Assets) - {initial}"

def strip_if_str(value):
    return value.strip() if isinstance(value, str) else value


def safe_strip(value):
    return value.strip() if isinstance(value, str) else value

def as_int(value):
    return 1 if value else 0


def is_sync_enabled(fieldname):
    """Return True if the given Erpnext Sbca Settings toggle is on.

    Defaults to True if the setting can't be read, or the field doesn't
    exist yet (preserves prior behaviour for freshly-migrated sites that
    haven't had a chance to opt out of anything yet).
    """
    import frappe
    try:
        value = frappe.db.get_single_value("Erpnext Sbca Settings", fieldname)
    except Exception:
        return True
    if value is None:
        return True
    return bool(value)


def ensure_party_group(group_doctype, group_name):
    """Return a valid leaf Customer/Supplier Group, creating it if missing.

    `group_doctype` is "Customer Group" or "Supplier Group"; `group_name` is
    the Sage category name for the party (may be blank or None).

    Returns:
      - None if `group_name` is blank, or if a same-named GROUP NODE
        (is_group=1) already exists -- a party cannot be assigned to a group
        node, so the caller should fall back to its own default.
      - the existing leaf group's name, if one already exists.
      - a freshly-created leaf group (placed flat under the doctype's root
        group, the same placement account.py uses for Sage accounts).

    Best-effort: any failure is logged and None is returned, so a category
    that cannot be created never aborts the customer/supplier sync.
    """
    import frappe

    name = group_name.strip() if isinstance(group_name, str) else group_name
    if not name:
        return None

    # "Customer Group" -> customer_group / parent_customer_group / customer_group_name
    snake = group_doctype.lower().replace(" ", "_")
    parent_field = "parent_" + snake
    name_field = snake + "_name"

    if frappe.db.exists(group_doctype, name):
        # A leaf -> use it. A group node -> a party cannot be assigned to it.
        is_group = frappe.db.get_value(group_doctype, name, "is_group")
        return None if is_group else name

    root = frappe.db.get_value(
        group_doctype,
        {"is_group": 1, parent_field: ["in", ["", None]]},
        "name",
        order_by="creation asc",
    )
    if not root:
        frappe.log_error(
            title=(
                f"Sage Sync: no root {group_doctype} -- cannot create '{name}'"
            )[:140],
            message=(
                f"No root {group_doctype} node found on this site, so the "
                f"Sage category '{name}' could not be created."
            ),
        )
        return None

    try:
        doc = frappe.get_doc(
            {
                "doctype": group_doctype,
                name_field: name,
                parent_field: root,
                "is_group": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception as e:
        frappe.log_error(
            title=f"Sage Sync: could not create {group_doctype} '{name}'"[:140],
            message=str(e),
        )
        return None
