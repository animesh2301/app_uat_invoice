# UAT build: BC → ContinuFlow tables, plus non-PO invoice posting

This build covers two things. BC data loads directly into the ContinuFlow dbo tables, and non-PO invoices post back to BC. PO-invoice posting is **not** part of this build.

Everything is scoped to `CLIENT_ID = 363` and `COMPANY_ID IN (889, 890)`. No statement in this app reads or writes another client's rows.

## Where each entity lands

| BC entity | Destination |
|---|---|
| vendors | `dbo.Mst_Vendors` |
| locations | `dbo.mst_store` |
| accounts (Expense only) | `dbo.COA_Master_SAP` |
| purchaseOrders + lines | `dbo.AP_DC_PurchaseOrder` + `dbo.AP_DC_PurchaseOrderLines` |
| purchaseReceipts + lines | `dbo.AP_DC_PurchaseReceipts` + `dbo.AP_DC_PurchaseReceiptLines` |
| paymentTerms, paymentMethods, items | `bc.stg_*`, because the dbo mappings look values up from them |
| purchaseInvoices + lines | `bc.stg_*`, unchanged |

The first five rows are written **directly**, with no `bc.stg_` copy. Their old `bc.stg_` tables (`stg_vendors`, `stg_locations`, `stg_accounts`, `stg_purchaseOrders`, `stg_purchaseOrderLines`, `stg_purchaseReceipts`, `stg_purchaseReceiptLines`) no longer receive updates. Once UAT signs off, they can be dropped.

Column mappings follow the CF team's SELECTs from the doc, column for column.

## How rows are written

Each row is UPDATEd by its key and INSERTed only if nothing matched:

| Table | Key |
|---|---|
| Mst_Vendors | CLIENT_ID, COMPANY_ID, BP_CODE |
| mst_store | CLIENT_ID, COMPANY_ID, PROFIT_CENTER_CODE |
| COA_Master_SAP | CLIENT_ID, COMPANY_ID, ACCOUNT_NUMBER (the BC account id) |
| AP_DC_PurchaseOrder | CompanyCode, POInternalID |
| AP_DC_PurchaseOrderLines | CLIENT_ID, COMPANY_ID, POInternalID (the line's BC id) |
| AP_DC_PurchaseReceipts / Lines | CLIENT_ID, COMPANY_ID, id |

There are two reasons rows are updated in place instead of deleted and re-inserted:

- **IDENTITY ids never change.** `mst_store.PROFIT_CENTER_ID` is referenced from `AP_DataConversions` and `AP_DC_GL_Details`. Deleting and re-inserting would give the store a new id on every BC edit and break those links.
- **Only BC-sourced columns are overwritten.** The columns the doc fills with NULL or constants (`GROUP_CODE`, `LEGAL_ENTITY`, `IS_ACTIVE` and so on) are set when the row is created and never touched again. Anything the CF team fills into them stays.

Created/updated columns follow one rule in every table: on create, both get the same value; after that, the created column never changes and only the updated column moves.

| Table | Created / Updated columns | Value |
|---|---|---|
| Mst_Vendors | CREATED_DATE / UPDATED_DATE | BC `lastModifiedDateTime` (as in the doc) |
| COA_Master_SAP | CREATED_DATE / UPDATED_DATE | BC `lastModifiedDateTime` (as in the doc) |
| AP_DC_PurchaseOrder | CreatedDate / ModifiedDate | BC `lastModifiedDateTime` (as in the doc) |
| mst_store | DT_CREATED_ON / DT_UPDATED_ON | time of the sync (as in the doc) |

Concurrent events for the same record are safe. Before writing, each record takes an `sp_getapplock` on its key (vendor code, store code, account id, PO id, receipt id). A second event for the same record waits for the first to commit, then updates the row instead of inserting a duplicate. The lock lives only in SQL Server's lock manager and touches no table, row or index, so it blocks nobody except another writer of that exact record.

Every BC call for a document (lines, location codes) happens **before** its first SQL write, so a slow or throttled BC response never holds a lock on the CF tables.

**No indexes are needed on the dbo tables.** Without them, each write scans its table instead of seeking the row. That's fine for small tables. `sql/dbo_indexes_optional.sql` has the indexes to request from the CF team if a table turns out to be large, plus a row-count query to decide.

Each document (PO or receipt, with its lines) is written in one transaction. If one line fails, the whole document is rolled back.

When a line is removed from a PO in BC, it is also removed from `AP_DC_PurchaseOrderLines`. Otherwise ContinuFlow could match an invoice against a line that no longer exists.

## Deletes

| BC delete | What happens |
|---|---|
| vendor | `Mst_Vendors.isActive` set to `0`. The row is kept. |
| purchase order | The `AP_DC_PurchaseOrder` row and all its `AP_DC_PurchaseOrderLines` are deleted. |
| purchase order line | That line is deleted from `AP_DC_PurchaseOrderLines`. |
| anything else | Logged, not applied. Pending the CF team. |

A delete event carries only the BC id, and by then the vendor is gone from BC, so its number can't be read back. Every vendor and location upsert therefore records its BC id → code pair in `bc.CfKeyMap`. Run `CfBackfill` once for `vendors` and `locations` after deploying to fill it for existing records. Until then, a delete falls back to the old `bc.stg_vendors` row.

If a deleted vendor is re-created or unblocked in BC, the next update sets `isActive` from BC's `blocked` field again, as before.

## Functions

| Function | Trigger | Purpose |
|---|---|---|
| `BcWebhookReceiver` | HTTP | Unchanged |
| `BcEventProcessor` | Event Hub | Writes the CF entities directly; everything else goes to `bc.stg_*` |
| `RenewBcSubscription` | Timer, every 12h | Renews every subscription for both companies (see below) |
| `PostNonPoInvoices` | Timer, every 5 min | Polls `AP_DataConversions` and posts invoices |
| `PostNonPoInvoicesNow` | HTTP | Manual trigger for UAT |
| `PendingInvoices` | HTTP GET | Read-only: shows what the timer would pick up |
| `CfBackfill` | HTTP | Re-reads records from BC and upserts them into the CF tables |

## Setup, in order

**1. Run `sql/uat_schema.sql`.** It only touches the `bc` schema: `bc.stg_paymentMethods`, `bc.stg_items`, `bc.InvoicePostLog`, `bc.CfKeyMap`, and their indexes. Nothing in `dbo` is created or altered. `sql/dbo_indexes_optional.sql` is a separate, optional request for the CF team.

**2. Add the app settings.**

```
CF_BC_COMPANY_MAP = {"889":"<bc-company-guid>","890":"<bc-company-guid>"}
CF_CLIENT_ID      = 363
CF_COMPANY_IDS    = [889, 890]
```

`CF_BC_COMPANY_MAP` is required in both directions. BC events carry BC's company GUID; the dbo tables use COMPANY_ID; and nothing links the two automatically. A BC company missing from the map is skipped.

**3. Register webhook subscriptions for `paymentMethods` and `items`** in both companies (889 and 890).

**Subscription renewal.** Every entity has two subscriptions, one per company. `RenewBcSubscription` handles this in one of two ways:

- **Auto (recommended):** leave `BC_SUBSCRIPTION_IDS` unset, or delete it. The function lists `/subscriptions` and renews every subscription whose `notificationUrl` points at this Function App (matched on the `WEBSITE_HOSTNAME` that Azure sets; override it with `BC_WEBHOOK_HOST` if you use a custom domain). A re-created subscription is picked up automatically.
- **Explicit list:** each value can be one id or a list:

  ```json
  {"vendors":["<id-889>","<id-890>"],"purchaseOrders":["<id-889>","<id-890>"]}
  ```

Renewal sends `If-Match` with the subscription's etag, as BC requires. An expired subscription can't be renewed and has to be re-created; the run fails with a log line naming it.

**4. Initial-load `bc.stg_paymentMethods` and `bc.stg_items`, then run `CfBackfill` for `vendors` and `locations`** (this fills `bc.CfKeyMap`). Webhooks only carry changes, so both tables start empty. Until they're loaded, `PAYMENT_METHOD_CODE` and the PO line item columns will be NULL.

## Backfill

Backfill runs **only when you call it**; there is no timer. Each dbo row is only as current as the last webhook that reached it. Use this endpoint in three situations:

- an event was missed or dead-lettered;
- a lookup value changed in BC (payment terms renamed, item category changed) without the vendor or PO itself changing;
- you've fixed whatever caused rows in `bc.FailedEvents`.

```bash
POST /api/cf/backfill?entity=vendors&code=KEY
POST /api/cf/backfill?entity=purchaseOrders&modifiedSince=2026-10-01&code=KEY
POST /api/cf/backfill?entity=locations&company=889&code=KEY
```

`entity` is one of `vendors`, `locations`, `accounts`, `purchaseOrders` or `purchaseReceipts`. Use `modifiedSince` for the two document entities, because they fetch every document's lines and the HTTP gateway cuts responses off at about 230 seconds.

The response gives read / inserted / updated / failed counts per company, plus the first 20 errors.

## Non-PO invoice posting

`AP_DataConversions` rows with `STATUS = 'APPROVED'`, `ERP_POST = 'N'` and a client/company in scope are picked up. The header comes from `AP_DC_InvoiceHeader`. **Every** `AP_DC_GL_Details` row for the record becomes an `Account` line, with no filtering. The function creates the BC purchase invoice, adds the dimensions to each line, then sets `ERP_POST = 'Y'`.

| Line value | Source |
|---|---|
| `accountId` | `COA_Master_SAP.ACCOUNT_NUMBER` where `ACCOUNT_CODE = GLCODE`, **and the invoice's own CLIENT_ID / COMPANY_ID**. The same account number exists in 889 and 890 with different BC ids. If it isn't found there, BC is queried in the invoice's company. Two different ids for one code in one company holds the invoice. |
| `COST CENTER` | `AP_DC_GL_Details.COST_CENTER` |
| `DIVISION` | `mst_store.PROFIT_CENTER_CODE`, joined on `PROFIT_CENTER_ID`. A store from another company, or an id with no store, holds the invoice. A line with no `PROFIT_CENTER_ID` is posted without a division. |

| Setting | Default | Notes |
|---|---|---|
| `AP_POST_MODE` | `draft` | `draft` creates the invoice; a BC user posts it. `post` also calls `Microsoft.NAV.post`. |
| `AP_POST_MAX_ATTEMPTS` | `3` | After this many failures, `ERP_POST` becomes `'E'` so the timer stops retrying. |
| `AP_POST_DIVISION` | `true` | Posts the DIVISION dimension. |
| `AP_UNITCOST_PATCH` | `false` | `unitCost` is sent in the line POST. Set `true` to also PATCH it afterwards, if BC ever overwrites it. |

On failure, the latest error goes to `AP_DataConversions.ERP_ERROR`, and the full history of attempts is in `bc.InvoicePostLog`:

```sql
SELECT Attempt, Step, Outcome, Detail, BcInvoiceNumber, LoggedAt
FROM bc.InvoicePostLog WHERE RecordId = 12345 ORDER BY LogId;
```

UAT runbook:

```bash
GET  /api/ap/pending?code=KEY                     # what's waiting
POST /api/ap/post-invoices?recordId=12345&code=KEY  # post one record
POST /api/ap/post-invoices?code=KEY               # post everything pending
```

## Implemented as in the doc, worth raising with the CF team

- **`AP_DC_PurchaseOrderLines.LineNumber`** comes from `lineObjectNumber`, which is the item number (the same value as `ItemNumber`). The real line number is `sequence`.
- **`COA_Master_SAP.INACTIVE`**: `blocked = FALSE` gives `'T'`, which looks inverted.
- **`mst_store.DT_CREATED_ON` / `DT_UPDATED_ON`** use the doc's `SYSUTCDATETIME() AT TIME ZONE 'Eastern Standard Time'`. That expression labels UTC time as Eastern without converting it, so the stored value is UTC. It's kept as-is so new rows match the historical ones.
- **`mst_store` and branches.** The doc LEFT JOINs `mst_branch`, which gives one store row per branch when a company has several. The direct load keeps one row per store code and uses the lowest `branch_id`. A check query is in the SQL file.

## Other open items

- **Deletes for the other entities** (locations, accounts, receipts): pending the CF team.
- **Invoice PDF / DocSolutions.** Not implemented yet. BC exposes `attachments` and `documentAttachments` on `purchaseInvoices`.
