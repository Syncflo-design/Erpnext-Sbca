# Copyright (c) 2026, Syncflo Design and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SageTax(Document):
    """Per-Company cache of Sage's tax catalogue.

    Populated by erpnext_sbca.API.tax.get_taxes_from_sage(), which calls
    /api/SalesTaxSync/get-sales-taxes-for-erpnext on each Company Sage
    Integration row and upserts one Sage Tax record per (template, child)
    pair returned. Rows that stop being returned by Sage get disabled = 1
    rather than deleted, so historical Item Tax Template mappings keep
    resolving but a warning can be surfaced.

    Used at push time by erpnext_sbca.API.tax.resolve_sage_tax(),
    which the items.py / sales_invoice.py / purchase_invoice.py /
    pos_invoice.py workers call to look up the correct sage_idx and rate
    for the active Company.
    """

    pass
