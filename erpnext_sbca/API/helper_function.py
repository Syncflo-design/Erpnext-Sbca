
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