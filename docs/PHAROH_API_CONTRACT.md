# Pharoh API Contract — InventorySyncController

**Status:** LIVE — deployed 2026-05-15
**ERPNext app version at last sync:** `erpnext_sbca` 0.0.7
**Pharoh side:** `SageErpNextAPI` — `Controllers/InventorySyncController.cs`

This is the contract between the ERPNext custom app `erpnext_sbca` and the Pharoh
middleware for everything inventory- and stock-related. It is the source of truth:
if the ERPNext code and this document disagree, one of them is a bug — check the
route strings in `erpnext_sbca/API/*.py` against the routes below.

## Controller merge — history

Pharoh used to have three separate controllers for this surface area:

- `InventorySyncController` — item + price pulls, new-item push
- `StockSyncController` — stock-level pull, disable-qty-tracking
- `StockAdjustmentSyncController` — financial stock-reconciliation push

As of 2026-05-15 all three were folded into a single `InventorySyncController`
(route prefix `api/InventorySync`). The old `/api/StockSync/*` and
`/api/StockAdjustmentSync/*` routes are **dead** — they no longer exist on the
server. One route was also renamed in the merge:
`post-stock-adjustment-to-sage` → `post-financial-stock-reconciliation-to-sage`.

ERPNext was repointed to match in `erpnext_sbca` 0.0.7.

## Auth model — common to every endpoint

`apikey` is always passed in the query string: `?apikey=<API_KEY>`. The apikey
encodes **which Sage company** the request is for — ERPNext never passes a
company identifier separately, and every response is scoped to that one company.

Sage login credentials travel in the request body. Two shapes are in use
depending on the endpoint (noted per-endpoint below):

- **Bare credential block** — `{loginName, loginPwd, useOAuth, sessionToken, provider}`
- **Wrapped** — `{"credentials": {<the same five fields>}, ...}`

In both, `useOAuth` decides the path: when `false`, Sage authenticates with
`loginName`/`loginPwd`; when `true`, with `sessionToken`/`provider`.

---

## Endpoints

### 1. `POST /api/InventorySync/get-inventory-for-erpnext`

Pull the item catalogue from Sage into ERPNext.

**Query:** `?apikey=<KEY>&lastDate=<yyyy-MM-dd>&skipQty=<int>`
**Body:** bare credential block.

**Consumed by two ERPNext functions, two different ways:**

- `item_details.py → get_inventory_from_sage()` — the real catalogue sync.
  Passes `skipQty` and paginates. Expects a **wrapped object** response:
  `{"items": [...], "totalResults": <int>, "returnedResults": <int>}`.
- `item_details.py → update_prices()` — price refresh. Omits `skipQty`, does
  **not** paginate, and treats the response as a **plain list** of item dicts.

> ⚠️ Known ERPNext-side quirk: the two callers expect different response
> envelopes from the same route. Pharoh currently satisfies both because the
> non-paginated call still returns something iterable, but this is fragile. If
> Pharoh ever standardises the response, `update_prices()` must be updated to
> read `.get("items")`. Not Pharoh's bug — flagged here so it isn't forgotten.

**Item object fields ERPNext reads:** `item_code`, `item_name`, `description`,
`item_group`, `stock_uom`, `standard_rate`, `standard_rate_incl`,
`valuation_rate`, `last_purchase_rate`, `is_sales_item`, `is_purchase_item`,
`disabled`, the Sage selection id (`SelectionID` / `selectionId` /
`custom_sage_selection_id` / `id` — first non-null wins), `tax_typeid_sales`,
and the physical/service flag used by `resolve_is_stock_item()` to set
`is_stock_item` once on item creation.

---

### 2. `POST /api/InventorySync/get-inventory-qtyonhand-for-erpnext`

Pull Sage's current on-hand quantity and cost figures — **informational only**,
it never touches ERPNext's stock ledger.

**Query:** `?apikey=<KEY>&lastDate=<yyyy-MM-dd>`
**Body:** bare credential block.
**Consumed by:** `item_details.py → get_item_inventory_qty_on_hand_from_sage(company)`
— the "Pull Stock On Hand" button on the Settings → Stock tab.

**Response:** a plain JSON **list**, one element per item:

```json
[
  { "code": "WIDGET-001", "averageCost": 18.75, "priceExclusive": 25.00,
    "lastCost": 17.90, "quantityOnHand": 42.0 }
]
```

ERPNext stamps `valuation_rate`, `standard_rate`, `last_purchase_rate`,
`custom_quantity_on_hand` onto matching Items. Must be a list — ERPNext checks
`isinstance(response, list)` and throws otherwise.

---

### 3. `POST /api/InventorySync/post-new-item-to-sage`

Push a newly-created ERPNext Item into Sage.

**Query:** `?apikey=<KEY>`
**Body:** wrapped credentials, plus an `item` object:

```json
{
  "credentials": { "loginName": "...", "loginPwd": "...", "useOAuth": false,
                   "sessionToken": "...", "provider": "..." },
  "item": {
    "ID": 0, "Code": "WIDGET-001", "Description": "...", "Active": true,
    "PriceExclusive": 25.00, "PriceInclusive": 28.75,
    "Physical": false,
    "TaxTypeIdSales": 1, "TaxTypeIdPurchases": 1,
    "Unit": "Each", "Created": "2026-05-15T10:00:00",
    "Modified": "2026-05-15T10:00:00", "LastCost": 17.90, "AverageCost": 18.75
  }
}
```

**Consumed by:** `items.py → _post_item_worker()` (doc_event on Item insert).

> `Physical` is **always `false`** — every item is created in Sage as a
> service / "Do Not Track Balance" item. ERPNext owns stock quantities; Sage
> only records the financial value of stock movements.

**Response:** `{ "id": <int> }` (or `"ID"`) — the Sage selection id, stamped
back onto the Item as `custom_sage_selection_id`.

---

### 4. `POST /api/InventorySync/post-item-adjustment-to-sage`

Exists on the merged controller but **not currently consumed by `erpnext_sbca`**.
Listed here for completeness — if a future ERPNext feature needs it, document
the shape at that point.

---

### 5. `POST /api/InventorySync/get-stock-levels-for-erpnext`

The Sage side of the one-time **stock cutover** — returns every item's current
on-hand quantity and unit cost so ERPNext can write a single Opening Stock
reconciliation. Called on demand (Settings → Stock → "Import Stock Levels"),
once per company, never polled.

**Query:** `?apikey=<KEY>`
**Body:** bare credential block.
**Consumed by:** `stock.py → import_stock_levels_from_sage()`.

**Response:** a plain JSON **list** (not wrapped). An empty list `[]` is valid.

```json
[
  { "item_code": "WIDGET-001", "quantity": 42.0, "valuation_rate": 18.75 }
]
```

`item_code` is the join key — must match the Item code ERPNext already holds.
`valuation_rate` must be Sage's real cost (drives the GL posting). ERPNext skips
rows with quantity 0 or no matching Item. On HTTP 200 it requires a list — an
error object returned with status 200 will make ERPNext throw.

---

### 6. `POST /api/InventorySync/disable-qty-tracking`

Second and final step of the stock cutover — tells Sage to stop tracking
quantities for a list of items (switch them to non-physical / "Do Not Track
Balance"). Called on demand (Settings → Stock → "Disable Sage Qty Tracking"),
once per company, only after the stock import for that company has completed.

**Query:** `?apikey=<KEY>`
**Body:** wrapped credentials, plus `itemCodes`:

```json
{
  "credentials": { "...": "..." },
  "itemCodes": ["WIDGET-001", "GADGET-114", "BOLT-M6-50"]
}
```

**Consumed by:** `stock.py → disable_sage_qty_tracking()`.

**Behaviour:** per-item and idempotent — an item already non-physical is a
success (no-op); an unknown item code goes into `errors`, it does not abort the
run. One bad code never blocks the rest.

**Response:** a JSON **object**:

```json
{ "success": true, "disabled": 41, "errors": ["BOLT-M6-50: item not found in Sage"] }
```

`success` is `true` if the request ran to completion (even with per-item
errors); `false` only on whole-request failure (auth, Sage unreachable, bad
JSON). ERPNext sets `sage_qty_tracking_disabled = 1` only on `success: true`.

---

### 7. `POST /api/InventorySync/post-financial-stock-reconciliation-to-sage`

> Renamed in the 2026-05-15 merge — was `post-stock-adjustment-to-sage` on the
> old `StockAdjustmentSyncController`.

Push an ERPNext stock-**value** movement to Sage as a Journal Entry. Fired on
submit of value-affecting Stock Entries (Material Issue/Receipt, Manufacture,
Material Consumption for Manufacture, Repack, Disassemble) and every Stock
Reconciliation **except** Opening Stock. Gated by the
`push_stock_adjustment_on_submit` toggle.

**Query:** `?apikey=<KEY>`
**Body:** wrapped credentials, plus `stockAdjustment`:

```json
{
  "credentials": { "...": "..." },
  "stockAdjustment": {
    "date": "2026-05-15", "reference": "STE-2026-00042",
    "entryType": "Material Issue", "description": "...", "memo": "...",
    "taxPeriodId": null, "analysisCategoryId1": null,
    "analysisCategoryId2": null, "analysisCategoryId3": null,
    "trackingCode": "", "businessId": null, "payRunId": null,
    "lines": [
      { "effect": 1, "accountId": 12345, "debit": 1500.00, "credit": 0,
        "exclusive": 1500.00, "tax": 0, "total": 1500.00,
        "taxTypeId": null, "description": "Stock In Hand" },
      { "effect": 2, "accountId": 67890, "debit": 0, "credit": 1500.00,
        "exclusive": 1500.00, "tax": 0, "total": 1500.00,
        "taxTypeId": null, "description": "Stock Adjustment" }
    ]
  }
}
```

**Consumed by:** `stock_adjustment.py → _post_stock_adjustment_worker()`.

**Shape rules:** `effect` is `1` = Debit, `2` = Credit. `accountId` is a signed
long (Sage uses negatives for system accounts — do not reject them). Nullable
integers arrive as `null` and must stay `null` (Sage treats `0` as an invalid
FK). The lines array is balanced (Σ debits = Σ credits) and **must contain at
least 2 lines** — Pharoh rejects fewer. Pharoh decomposes the lines into
N-1 Sage `JournalEntry/Save` calls under one shared `Reference` (pivot
decomposition on `lines[0]`).

**Response:**

```json
{ "success": true, "sageOrderId": "...", "documentNumber": "...", "errorMessage": null }
```

On failure: `success: false`, ids null, `errorMessage` populated. ERPNext
stamps `custom_sage_order_id` / `custom_sage_document_number` /
`custom_sage_sync_status` on the submitted document from the response.

---

## ERPNext-side route map (quick reference)

| Route | ERPNext file → function |
|---|---|
| `get-inventory-for-erpnext` | `item_details.py` → `get_inventory_from_sage()`, `update_prices()` |
| `get-inventory-qtyonhand-for-erpnext` | `item_details.py` → `get_item_inventory_qty_on_hand_from_sage()` |
| `post-new-item-to-sage` | `items.py` → `_post_item_worker()` |
| `post-item-adjustment-to-sage` | *(not consumed)* |
| `get-stock-levels-for-erpnext` | `stock.py` → `import_stock_levels_from_sage()` |
| `disable-qty-tracking` | `stock.py` → `disable_sage_qty_tracking()` |
| `post-financial-stock-reconciliation-to-sage` | `stock_adjustment.py` → `_post_stock_adjustment_worker()` |

When Pharoh changes an inventory route, update this file and the matching route
string in the ERPNext file above — nothing else references these URLs.
