# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ItemTaxTemplateSageMap(Document):
    """Child rows on Item Tax Template that pair an ERPNext tax template
    with the Sage Tax records to use per Company.

    One row per (Item Tax Template, Company) pair. The row tells the push
    workers: 'When pushing a line referencing an item tagged with this
    template, to Sage tenant X, use sales_sage_tax on sales-direction
    pushes and purchase_sage_tax on purchase-direction pushes.'

    Resolved at runtime by erpnext_sbca.API.tax.resolve_sage_tax(), which
    is called by items.py, sales_invoice.py, purchase_invoice.py and
    pos_invoice.py for every line they send.
    """

    pass
