import json
import logging
import os
import re

import azure.functions as func
import pymssql
import requests
from azure.eventhub import EventData
from azure.eventhub.aio import EventHubProducerClient
from typing import List
from bc_auth import get_bc_token
from bc_http import bc_get, bc_get_all, bc_request
import cf_direct
import invoice_poster
import socket
import uat_config as uat

app = func.FunctionApp()

# ---------------------------------------------------------------------------
# Shared config / helpers — used by more than one function below
# ---------------------------------------------------------------------------

EXPECTED_CLIENT_STATE = os.environ["BC_CLIENT_STATE"]
EVENTHUB_CONN_STR = os.environ["EventHubConnection"]
EVENTHUB_NAME = "bc-ds-events"

# pymssql bundles its own driver (FreeTDS) inside the pip wheel — no ODBC Driver
# 18 install needed locally or on Azure, unlike pyodbc. Connect with discrete
# params rather than a single connection string.
SQL_SERVER = os.environ["SQL_SERVER"]      # "<server>.database.windows.net"
SQL_DATABASE = os.environ["SQL_DATABASE"]
SQL_USER = os.environ["SQL_USER"]
SQL_PASSWORD = os.environ["SQL_PASSWORD"]

# Base for every BC API call made from this app. Previously hardcoded to
# "D365-BC-QA" inside fetch_from_bc; now driven by the same BC_ENVIRONMENT
# app setting the renewal timer already uses, with the old value as fallback
# so nothing breaks if that setting is missing.
BC_API_BASE = (
    "https://api.businesscentral.dynamics.com/v2.0/"
    f"{os.environ.get('BC_ENVIRONMENT', 'D365-BC-QA')}"
)

BC_HTTP_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Entity registry — the one place that knows about each BC entity.
#
# Key = the entity name exactly as it appears in the webhook "resource" path,
# e.g. ".../companies(...)/vendors(...)" -> key "vendors".
#
# To add a new entity: add one entry here (table name + column list matching
# the BC entity's field names) and register the webhook subscription against
# that entity's resource path yourself. Nothing else in this file changes.
#
# IMPORTANT: the "purchaseOrders" column list below is the standard BC API
# v2.0 field set — cross-check it against whatever you already created for
# dbo.BCPurchaseOrders, since that table may already exist with a different
# column set than this reference list.
# ---------------------------------------------------------------------------

ENTITY_CONFIG = {
    "vendors": {
        "table": "bc.stg_vendors",
        "columns": [
            "addressLine1", "addressLine2", "balance", "blocked", "city",
                  "country", "currencyCode", "currencyId", "displayName", "email",
                  "id", "irs1099Code", "lastModifiedDateTime", "number",
                  "paymentMethodId", "paymentTermsId", "phoneNumber", "postalCode",
                  "state", "taxLiable", "taxRegistrationNumber", "website"
        ],
    },
    "purchaseOrders": {
        "table": "bc.stg_purchaseOrders",
        "columns": [
            "buyFromAddressLine1","buyFromAddressLine2","buyFromCity","buyFromCountry",
                           "buyFromPostCode","buyFromState","currencyCode","currencyId","discountAmount",
                           "discountAppliedBeforeTax","fullyReceived","id","lastModifiedDateTime","number",
                           "orderDate","payToAddressLine1","payToAddressLine2","payToCity","payToCountry",
                           "payToName","payToPostCode","payToState","payToVendorId","payToVendorNumber",
                           "paymentTermsId","postingDate","pricesIncludeTax","purchaser","requestedReceiptDate",
                           "shipToAddressLine1","shipToAddressLine2","shipToCity","shipToContact","shipToCountry",
                           "shipToName","shipToPostCode","shipToState","shipmentMethodId","shortcutDimension1Code",
                           "shortcutDimension2Code","status","totalAmountExcludingTax","totalAmountIncludingTax",
                           "totalTaxAmount","vendorId","vendorName","vendorNumber"
        ],
    },
    "accounts": {
        "table": "bc.stg_accounts",
        "columns": ["accountType","blocked","category","consolidationCreditAccount",
                    "consolidationDebitAccount","consolidationTranslationMethod","directPosting",
                    "displayName","excludeFromConsolidation","id","lastModifiedDateTime","netChange",
                    "number","subCategory"
                    ],
    },
    "locations": {
        "table": "bc.stg_locations",
        "columns": ["addressLine1" ,"addressLine2" ,"city" ,"code" ,"contact" ,"country" ,
                    "displayName" ,"email" ,"id" ,"lastModifiedDateTime" ,"phoneNumber" ,
                    "postalCode" ,"state" ,"website"
                    ],
    },
    "paymentTerms": {
        "table": "bc.stg_paymentTerms",
        "columns": ["calculateDiscountOnCreditMemos" ,"code" ,"discountDateCalculation" ,"discountPercent",
                    "displayName" ,"dueDateCalculation" ,"id" ,"lastModifiedDateTime"
                    ],
    },
    "purchaseReceiptLines": {
        "table": "bc.stg_purchaseReceiptLines",
        "columns": ["description" ,"description2" ,"discountPercent" ,"documentId" ,"expectedReceiptDate" ,
                    "id" ,"lineObjectNumber" ,"lineType" ,"quantity" ,"sequence" ,"taxPercent" ,
                    "unitCost" ,"unitOfMeasureCode"
                    ],
    },
    "purchaseReceipts": {
        "table": "bc.stg_purchaseReceipts",
        "columns": ["buyFromAddressLine1" ,"buyFromAddressLine2" ,"buyFromCity" ,"buyFromCountry" ,"buyFromPostCode",
                    "buyFromState" ,"currencyCode" ,"dueDate" ,"id" ,"invoiceDate" ,"lastModifiedDateTime" ,"number" ,
                    "orderNumber" ,"payToAddressLine1" ,"payToAddressLine2" ,"payToCity" ,"payToContact" ,"payToCountry"
                    ,"payToName" ,"payToPostCode" ,"payToState" ,"payToVendorNumber" ,"postingDate" ,"shipToAddressLine1"
                    ,"shipToAddressLine2" ,"shipToCity" ,"shipToContact" ,"shipToCountry" ,"shipToName" ,"shipToPostCode"
                    ,"shipToState" ,"vendorName" ,"vendorNumber"]
    },

    # -----------------------------------------------------------------------
    # FIXED: this list was previously an exact copy of the purchaseReceipts
    # header columns (buyFromAddressLine1, vendorName, ...). None of those
    # fields exist on a purchase order LINE, so every line row would have been
    # written as all-NULLs. Replaced with the BC API v2.0 purchaseOrderLines
    # field set.
    # -----------------------------------------------------------------------
    "purchaseOrderLines": {
        "table": "bc.stg_purchaseOrderLines",
        "columns": ["id" ,"documentId" ,"sequence" ,"itemId" ,"accountId" ,"lineType" ,
                    "lineObjectNumber" ,"description" ,"description2" ,"unitOfMeasureId" ,
                    "unitOfMeasureCode" ,"quantity" ,"directUnitCost" ,"discountAmount" ,
                    "discountPercent" ,"discountAppliedBeforeTax" ,"amountExcludingTax" ,
                    "taxCode" ,"taxPercent" ,"totalTaxAmount" ,"amountIncludingTax" ,
                    "invoiceDiscountAllocation" ,"netAmount" ,"netTaxAmount" ,
                    "netAmountIncludingTax" ,"expectedReceiptDate" ,"receivedQuantity" ,
                    "invoicedQuantity" ,"invoiceQuantity" ,"receiveQuantity" ,
                    "itemVariantId" ,"locationId"]
    },
    "purchaseInvoices": {
        "table": "bc.stg_purchaseInvoices",
        "columns": ["buyFromAddressLine1" ,"buyFromAddressLine2" ,"buyFromCity" ,"buyFromCountry" ,
                    "buyFromPostCode" ,"buyFromState" ,"currencyCode" ,"currencyId" ,"discountAmount" ,
                    "discountAppliedBeforeTax" ,"dueDate" ,"id" ,"invoiceDate" ,"lastModifiedDateTime" ,
                    "number" ,"orderId" ,"orderNumber" ,"payToAddressLine1" ,"payToAddressLine2" ,"payToCity" ,
                    "payToContact" ,"payToCountry" ,"payToName" ,"payToPostCode" ,"payToState" ,"payToVendorId" ,
                    "payToVendorNumber" ,"postingDate" ,"pricesIncludeTax" ,"purchaser" ,"shipToAddressLine1" ,
                    "shipToAddressLine2" ,"shipToCity" ,"shipToContact" ,"shipToCountry" ,"shipToName" ,
                    "shipToPostCode" ,"shipToState" ,"shortcutDimension1Code" ,"shortcutDimension2Code" ,
                    "status" ,"totalAmountExcludingTax" ,"totalAmountIncludingTax" ,"totalTaxAmount" ,"vendorId" ,
                    "vendorInvoiceNumber" ,"vendorName" ,"vendorNumber"]
    },

    # -----------------------------------------------------------------------
    # NEW. Invoice lines use unitCost (NOT directUnitCost, which is an order
    # line field), and have none of the receive/invoice quantity tracking
    # fields that order lines carry.
    # -----------------------------------------------------------------------
    "purchaseInvoiceLines": {
        "table": "bc.stg_purchaseInvoiceLines",
        "columns": ["id" ,"documentId" ,"sequence" ,"itemId" ,"accountId" ,"lineType" ,
                    "lineObjectNumber" ,"description" ,"description2" ,"unitOfMeasureId" ,
                    "unitOfMeasureCode" ,"quantity" ,"unitCost" ,"discountAmount" ,
                    "discountPercent" ,"discountAppliedBeforeTax" ,"amountExcludingTax" ,
                    "taxCode" ,"taxPercent" ,"totalTaxAmount" ,"amountIncludingTax" ,
                    "invoiceDiscountAllocation" ,"netAmount" ,"netTaxAmount" ,
                    "netAmountIncludingTax" ,"expectedReceiptDate" ,"itemVariantId" ,
                    "locationId"]
    },

    # -----------------------------------------------------------------------
    # NOTE: vendors, locations, accounts, purchaseOrders(+Lines) and
    # purchaseReceipts(+Lines) above are kept here only so the "known entity"
    # check passes. Their events are routed to cf_direct and written straight
    # into the ContinuFlow dbo tables - their bc.stg_ tables are no longer
    # updated. purchaseInvoices(+Lines) and paymentTerms still land in bc.stg_.
    #
    # Added for the ContinuFlow mappings, which look up values from
    # bc.stg_paymentMethods (Mst_Vendors.PAYMENT_METHOD_CODE,
    # AP_DC_PurchaseOrder.PaymentMethod) and bc.stg_items
    # (AP_DC_PurchaseOrderLines.ItemNumber / ItemType / Category), so both
    # have to be landed. Register a webhook subscription for each and do an
    # initial load - see README.
    # -----------------------------------------------------------------------
    "paymentMethods": {
        "table": "bc.stg_paymentMethods",
        "columns": ["id", "code", "displayName", "lastModifiedDateTime"],
    },
    "items": {
        "table": "bc.stg_items",
        "columns": ["id", "number", "displayName", "type", "itemCategoryId",
                    "itemCategoryCode", "blocked", "baseUnitOfMeasureId",
                    "baseUnitOfMeasureCode", "gtin", "inventory",
                    "unitPrice", "priceIncludesTax", "unitCost",
                    "taxGroupId", "taxGroupCode",
                    "generalProductPostingGroupId",
                    "generalProductPostingGroupCode",
                    "inventoryPostingGroupId", "inventoryPostingGroupCode",
                    "lastModifiedDateTime"],
    },
    # Add further entities here, e.g.:
    # "salesOrders": {
    #     "table": "dbo.BCSalesOrders",
    #     "columns": [...],
    # },
}

# ---------------------------------------------------------------------------
# Child-line registry — parent entity -> the line collection to keep in sync.
#
# BC does not expose webhook subscriptions for purchaseOrderLines or
# purchaseInvoiceLines. It does, however, bump the PARENT document's
# lastModifiedDateTime whenever one of its lines is added, changed or removed,
# so the parent's webhook fires. That parent event is the trigger used here:
# after the header row is upserted, the document's full line set is re-read
# from BC and rewritten.
#
#   child_entity  BC navigation property on the parent, and the ENTITY_CONFIG
#                 key holding that line table's name + columns
#   link_column   column on the LINE table holding the parent document's id
#                 (BC calls this documentId on both line entities)
#
# To cover another document type, add an entry — nothing below changes.
# ---------------------------------------------------------------------------

CHILD_ENTITY_CONFIG = {
    "purchaseOrders": {
        "child_entity": "purchaseOrderLines",
        "link_column": "documentId",
    },
    "purchaseInvoices": {
        "child_entity": "purchaseInvoiceLines",
        "link_column": "documentId",
    },
}

COMPANY_CONFIG: dict[str, str] = json.loads(os.environ.get("BC_COMPANY_ID", "{}"))

def extract_company_entity_and_id(resource: str) -> tuple[str, str, str] | tuple[None, None, None]:
    # resource looks like: api/v2.0/companies(...)/vendors(26814998-936a-401c-81c1-0e848a64971d)
    # or                    api/v2.0/companies(...)/purchaseOrders(...)
    # Captures the last "<entityName>(<guid>)" segment regardless of which entity it is.
    m = re.search(r"companies\(([0-9a-fA-F-]+)\)/([A-Za-z]+)\(([0-9a-fA-F-]+)\)\s*$", resource)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3)


def get_sql_connection():
    return pymssql.connect(
        server=SQL_SERVER,
        user=SQL_USER,
        password=SQL_PASSWORD,
        database=SQL_DATABASE,
    )


class SqlSession:
    """
    One SQL connection reused across a whole Event Hub batch. Previously every
    helper opened and closed its own, so a batch of 100 events meant 100+ TLS
    handshakes to Azure SQL. reset() drops a connection that has gone bad so
    the rest of the batch can continue.
    """

    def __init__(self):
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = get_sql_connection()
        return self._conn

    def reset(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            logging.debug("Ignoring error closing a SQL connection",
                          exc_info=True)
        finally:
            self._conn = None

    close = reset


def _session(sql):
    """(session, owned) - callers that pass one keep ownership of it."""
    if sql is not None:
        return sql, False
    return SqlSession(), True


def coerce(value):
    """
    Match the existing upsert behaviour: booleans go in as the strings
    "True"/"False" (the columns are text), and a nested object or array is
    dropped rather than stringified into something unusable.
    """
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, (dict, list)):
        return None
    return value


# ---------------------------------------------------------------------------
# Function 1 — BC Webhook Receiver (HTTP trigger)
#   Handles the validation handshake, and forwards real notifications to
#   Event Hub. One endpoint, shared by every entity's webhook subscription —
#   BC tells you which entity a given notification is for via its "resource"
#   path, so nothing entity-specific happens here beyond partitioning.
# ---------------------------------------------------------------------------

@app.route(route="BcWebhookReceiver", auth_level=func.AuthLevel.FUNCTION, methods=["POST"])
async def BcWebhookReceiver(req: func.HttpRequest) -> func.HttpResponse:
    validation_token = req.params.get("validationToken")
    if validation_token:
        return func.HttpResponse(validation_token, status_code=200, mimetype="text/plain")

    if req.params.get("dnstest"):
        hosts = [
            "04-funcapp-cfv2-dev-cfbpeshkf2bqgre3.eastus2-01.azurewebsites.net",
            "04strgacccfv2dev.blob.core.windows.net",
            "04strgacccfv2dev.queue.core.windows.net",
            "EUS2-EH-CFv2-DEV.servicebus.windows.net",
        ]
        results = {}
        for host in hosts:
            try:
                results[host] = socket.gethostbyname(host)
            except Exception as e:
                results[host] = f"ERROR: {e}"
        return func.HttpResponse(json.dumps(results, indent=2), status_code=200, mimetype="application/json")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Bad request", status_code=400)

    items = body.get("value", [])
    to_send = [i for i in items if i.get("clientState") == EXPECTED_CLIENT_STATE]

    dropped = len(items) - len(to_send)
    if dropped:
        logging.warning("Dropped %d event(s) with mismatched clientState", dropped)

    if to_send:
        producer = EventHubProducerClient.from_connection_string(
            EVENTHUB_CONN_STR, eventhub_name=EVENTHUB_NAME
        )
        async with producer:
            for item in to_send:
                _, _, record_id = extract_company_entity_and_id(item.get("resource", ""))
                # partition_key groups all events for the same record together,
                # regardless of which entity it belongs to. For a document whose
                # lines are synced from its header event, this also keeps two
                # rapid edits to the same order on one partition, so they are
                # processed in order and the later line snapshot wins.
                batch = await producer.create_batch(partition_key=record_id)
                batch.add(EventData(json.dumps(item)))
                await producer.send_batch(batch)

    return func.HttpResponse(status_code=200)


# ---------------------------------------------------------------------------
# Function 2 — Event Processor (Event Hub trigger)
#   Consumes what Function 1 forwarded, fetches current state from BC when
#   needed, and upserts/deletes rows in the right table based on which
#   entity the event's resource path names.
# ---------------------------------------------------------------------------

def fetch_from_bc(resource_path: str) -> dict:
    # Through bc_http, which backs off on 429/Retry-After. BC allows only
    # FIVE concurrent API requests per environment; the previous
    # raise_for_status() turned a throttle into a dead-lettered event, so the
    # record silently never synced.
    return bc_get(f"{BC_API_BASE}/{resource_path.lstrip('/')}",
                  timeout=BC_HTTP_TIMEOUT)


def fetch_child_records(parent_resource: str, child_entity: str) -> list[dict]:
    """
    Read every line of one document, following @odata.nextLink.

    Deliberately a separate call rather than $expand on the parent fetch: an
    expanded collection can be silently truncated on a large document, while
    the dedicated endpoint pages properly. A 404 means the document is already
    gone in BC — treated as "no lines", which then clears the SQL side.
    """
    url = f"{BC_API_BASE}/{parent_resource.lstrip('/')}/{child_entity}"
    lines = bc_get_all(url, allow_404=True, timeout=BC_HTTP_TIMEOUT)

    if not lines:
        logging.info("No %s returned for %s", child_entity, parent_resource)

    return lines


def upsert_record(entity: str, record: dict, company_name: str,
                  sql: "SqlSession | None" = None) -> None:
    config = ENTITY_CONFIG[entity]
    table = config["table"]
    columns = config["columns"]+["company"]

    sql, owned = _session(sql)
    conn = sql.conn
    try:
        cursor = conn.cursor()
        update_clause = ", ".join(f"{c} = %({c})s" for c in columns if c != "id")
        insert_cols = ", ".join(columns)
        insert_vals = ", ".join(f"%({c})s" for c in columns)

        params = {c: coerce(record.get(c)) for c in columns}

        params["company"] = company_name

        cursor.execute(
            f"""
            MERGE {table} AS target
            USING (SELECT %(id)s AS id) AS src
            ON target.id = src.id
            WHEN MATCHED THEN UPDATE SET {update_clause}
            WHEN NOT MATCHED THEN
                INSERT ({insert_cols})
                VALUES ({insert_vals});
            """,
            params,
        )
        conn.commit()
    finally:
        if owned:
            sql.close()


def sync_child_lines(parent_entity: str, parent_resource: str,
                     parent_id: str, company_name: str,
                     sql: "SqlSession | None" = None) -> None:
    """
    Replace this document's line rows with what BC currently holds.

    Delete-then-insert rather than a per-line MERGE, because a MERGE alone
    cannot see a line that was REMOVED in BC — those rows would linger in SQL
    forever. Both statements run in one transaction, so a failure mid-way
    rolls back and leaves the previous line set intact rather than an empty
    document.
    """
    child_cfg = CHILD_ENTITY_CONFIG[parent_entity]
    child_entity = child_cfg["child_entity"]
    link_column = child_cfg["link_column"]

    line_cfg = ENTITY_CONFIG[child_entity]
    table = line_cfg["table"]
    columns = line_cfg["columns"] + ["company"]

    lines = fetch_child_records(parent_resource, child_entity)

    sql, owned = _session(sql)
    conn = sql.conn
    try:
        cursor = conn.cursor()

        cursor.execute(
            f"DELETE FROM {table} WHERE {link_column} = %s AND company = %s",
            (parent_id, company_name),
        )
        removed = cursor.rowcount

        if lines:
            insert_cols = ", ".join(columns)
            placeholders = ", ".join("%s" for _ in columns)

            rows = [
                tuple(
                    company_name if c == "company" else coerce(line.get(c))
                    for c in columns
                )
                for line in lines
            ]

            cursor.executemany(
                f"INSERT INTO {table} ({insert_cols}) VALUES ({placeholders})",
                rows,
            )

        conn.commit()

        logging.info(
            "Synced %s for %s %s (%s): %s row(s) replaced %s",
            child_entity, parent_entity, parent_id, company_name,
            len(lines), removed,
        )
    except Exception:
        try:
            conn.rollback()
        except Exception:
            sql.reset()
        raise
    finally:
        if owned:
            sql.close()


def delete_record(entity: str, record_id: str, company_name: str,
                  sql: "SqlSession | None" = None) -> None:
    table = ENTITY_CONFIG[entity]["table"]
    sql, owned = _session(sql)
    conn = sql.conn
    try:
        cursor = conn.cursor()
        # No IsDeleted flag in this schema — hard delete on the "deleted" changeType.
        cursor.execute(f"DELETE FROM {table} WHERE id = %s AND company = %s", (record_id, company_name))
        conn.commit()
    finally:
        if owned:
            sql.close()


def delete_child_lines(parent_entity: str, parent_id: str, company_name: str,
                       sql: "SqlSession | None" = None) -> None:
    """
    Deleting a header leaves its lines orphaned — BC sends no separate event
    for them, so they are cleared here.
    """
    child_cfg = CHILD_ENTITY_CONFIG[parent_entity]
    line_cfg = ENTITY_CONFIG[child_cfg["child_entity"]]

    sql, owned = _session(sql)
    conn = sql.conn
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"DELETE FROM {line_cfg['table']} "
            f"WHERE {child_cfg['link_column']} = %s AND company = %s",
            (parent_id, company_name),
        )
        conn.commit()
        logging.info(
            "Deleted %s row(s) from %s for %s %s",
            cursor.rowcount, line_cfg["table"], parent_entity, parent_id,
        )
    finally:
        if owned:
            sql.close()


def dead_letter(raw_payload: str, error: Exception) -> None:
    conn = get_sql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO bc.FailedEvents (Payload, [Error], FailedOn) VALUES (%s, %s, SYSUTCDATETIME())",
            (raw_payload, str(error)),
        )
        conn.commit()
    finally:
        conn.close()


def handle_bulk_change(item: dict) -> None:
    # "collection" fires when >1000 records changed within 30s — the payload
    # carries a filter instead of individual keys. Stubbed for POC; implement
    # a paged catch-up sync before relying on this for bulk BC updates.
    logging.warning("Bulk change received — implement paged catch-up sync. filter=%s", item.get("resource"))


def process_event(raw_body: str, sql: "SqlSession | None" = None) -> None:
    item = json.loads(raw_body)
    change_type = item.get("changeType")
    resource = item.get("resource", "")

    if change_type == "collection":
        handle_bulk_change(item)
        return

    company_id, entity, record_id = extract_company_entity_and_id(resource)
    if entity is None:
        logging.warning("Could not parse entity/id from resource: %s", resource)
        return

    if entity not in ENTITY_CONFIG:
        # A subscription exists for an entity this function doesn't know how to
        # store yet — surfaces loudly instead of silently dropping the event.
        raise ValueError(f"No ENTITY_CONFIG entry for entity '{entity}' (resource={resource})")

    company_name = COMPANY_CONFIG.get(company_id)
    if company_name is None:
        # Same idea as the entity check above — a company we don't have a name
        # for yet (a new company added in BC before BC_COMPANIES is updated)
        # dead-letters instead of writing a row stamped with a raw guid.
        raise ValueError(f"No BC_COMPANIES entry for company id '{company_id}' (resource={resource})")


    sql, owned_session = _session(sql)

    try:
        if entity in cf_direct.DIRECT_ENTITIES:
            # Entities in the CF doc load straight into the dbo tables
            # (Mst_Vendors, mst_store, COA_Master_SAP, AP_DC_*). No bc.stg_
            # copy is written for them.
            cf_direct.handle_event(sql.conn, change_type, entity,
                                   company_id, record_id)
            return

        _apply_event(change_type, entity, record_id, resource, company_name,
                     sql)
    finally:
        if owned_session:
            sql.close()


def _apply_event(change_type, entity, record_id, resource, company_name, sql):
    """Everything not in the CF doc still lands in bc.stg_* as before."""
    if change_type == "deleted":
        delete_record(entity, record_id, company_name, sql)

        if entity in CHILD_ENTITY_CONFIG:
            delete_child_lines(entity, record_id, company_name, sql)
        return

    record = fetch_from_bc(resource)
    upsert_record(entity, record, company_name, sql)

    # Lines have no webhook of their own. Editing a line bumps the parent
    # document's lastModifiedDateTime, so this event is the only signal that
    # the line set may have changed — and BC gives no way to tell a
    # header-only edit from a line edit. The lines are therefore re-read on
    # every parent event. Runs AFTER the header upsert so the line rows never
    # point at a document row that does not exist yet.
    if entity in CHILD_ENTITY_CONFIG:
        sync_child_lines(entity, resource, record_id, company_name, sql)


@app.function_name(name="BcEventProcessor")
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="bc-ds-events",
    connection="EventHubConnection",
    cardinality="many",
)
def BcEventProcessor(events: List[func.EventHubEvent]) -> None:
    if isinstance(events, func.EventHubEvent):
        events = [events]

    sql = SqlSession()
    failed = 0

    try:
        for event in events:
            raw = event.get_body().decode("utf-8")
            try:
                process_event(raw, sql)
            except Exception as ex:
                failed += 1
                logging.exception("Failed to process event")
                # dead_letter opens its own connection on purpose: the batch
                # connection may be the thing that just broke.
                dead_letter(raw, ex)
                sql.reset()
    finally:
        sql.close()

    if failed:
        logging.warning("Batch of %d event(s) finished with %d failure(s)",
                        len(events), failed)


# ---------------------------------------------------------------------------
# Renewal — Timer trigger
#   BC webhook subscriptions expire after ~3 days; this renews them every 12h.
#   One failing renewal doesn't block the others.
#
#   Which subscriptions are renewed:
#
#   1. BC_SUBSCRIPTION_IDS unset or empty  ->  AUTO (recommended).
#      Lists /subscriptions and renews every one whose notificationUrl points
#      at this Function App. Both companies' subscriptions are included -
#      subscriptions live at environment level, not under a company - and a
#      subscription you re-create picks up a new id with no setting to edit.
#
#   2. BC_SUBSCRIPTION_IDS set  ->  renews exactly those ids. Each value may be
#      one id or a list, so one entity can have a subscription per company:
#        {"vendors": ["<id-889>", "<id-890>"],
#         "purchaseOrders": ["<id-889>", "<id-890>"],
#         "paymentTerms": "<single-id>"}
#
#   NOTE: no subscription is needed for purchaseOrderLines or
#   purchaseInvoiceLines — they ride on their parent document's subscription.
# ---------------------------------------------------------------------------


def _subscriptions_base() -> str:
    return (
        f"https://api.businesscentral.dynamics.com/v2.0/{os.environ['BC_TENANT_ID']}/"
        f"{os.environ['BC_ENVIRONMENT']}/api/v2.0/subscriptions"
    )


def _configured_subscription_ids() -> list[tuple[str, str]]:
    """BC_SUBSCRIPTION_IDS -> [(label, subscription_id)]. Accepts str or list values."""
    raw = json.loads(os.environ.get("BC_SUBSCRIPTION_IDS", "") or "{}")
    pairs: list[tuple[str, str]] = []
    for entity, value in raw.items():
        ids = value if isinstance(value, list) else [value]
        for sub_id in ids:
            if sub_id:
                pairs.append((entity, str(sub_id)))
    return pairs


def _our_subscriptions(base: str) -> list[dict]:
    """Every subscription whose notificationUrl points at this Function App."""
    rows = bc_get_all(base)
    host = (os.environ.get("BC_WEBHOOK_HOST")
            or os.environ.get("WEBSITE_HOSTNAME", "")).lower()
    if not host:
        logging.warning("WEBSITE_HOSTNAME not set - renewing every subscription "
                        "this app registration can see (%d).", len(rows))
        return rows
    return [r for r in rows if host in (r.get("notificationUrl") or "").lower()]


def _renew(base: str, sub: dict) -> dict | None:
    """
    PATCH one subscription. BC requires If-Match with the subscription's
    etag, and re-validates notificationUrl (the receiver answers the
    validationToken handshake, as it did when the subscription was created).
    """
    body = {k: sub[k] for k in ("notificationUrl", "resource", "clientState")
            if sub.get(k)}
    return bc_request(
        "PATCH",
        f"{base}('{sub['subscriptionId']}')",
        json_body=body,
        etag=sub.get("@odata.etag") or "*",
        timeout=30,
    )


def _company_of(resource: str) -> str:
    m = re.search(r"companies\(([^)]+)\)", resource or "")
    return m.group(1) if m else "?"


@app.function_name(name="RenewBcSubscription")
@app.timer_trigger(schedule="0 0 */12 * * *", arg_name="timer", run_on_startup=False)
def RenewBcSubscription(timer: func.TimerRequest) -> None:
    base = _subscriptions_base()
    configured = _configured_subscription_ids()

    missing = 0
    if configured:
        targets = []
        for label, sub_id in configured:
            try:
                sub = bc_get(f"{base}('{sub_id}')", allow_404=True)
            except Exception:
                logging.exception("Failed to read %s subscription (%s)", label, sub_id)
                missing += 1
                continue
            if sub is None:
                logging.error(
                    "%s subscription %s no longer exists in BC (expired or deleted). "
                    "Re-create it and update BC_SUBSCRIPTION_IDS.", label, sub_id)
                missing += 1
                continue
            targets.append((label, sub))
        mode = "BC_SUBSCRIPTION_IDS"
    else:
        try:
            subs = _our_subscriptions(base)
        except Exception:
            logging.exception("Failed to list BC subscriptions")
            return
        targets = [((s.get("resource") or "").rsplit("/", 1)[-1], s) for s in subs]
        mode = "auto"

    if not targets and not missing:
        logging.warning("RenewBcSubscription (%s): nothing to renew.", mode)
        return

    renewed, failed = 0, missing
    for label, sub in targets:
        sub_id = sub.get("subscriptionId")
        company = _company_of(sub.get("resource"))
        try:
            resp = _renew(base, sub) or {}
            renewed += 1
            logging.info("Renewed %s subscription %s (company %s), new expiration: %s",
                         label, sub_id, company, resp.get("expirationDateTime"))
        except Exception:
            failed += 1
            logging.exception("Failed to renew %s subscription %s (company %s)",
                              label, sub_id, company)

    logging.info("RenewBcSubscription (%s): %d renewed, %d failed", mode, renewed, failed)
    if failed:
        # Surfaces as a failed run in the portal / App Insights
        raise RuntimeError(f"{failed} subscription renewal(s) failed")


# ---------------------------------------------------------------------------
# UAT SCOPE - non-PO invoice posting (ContinuFlow tables -> BC)
#
#   AP_DataConversions  STATUS='APPROVED', ERP_POST='N', client/company in
#                       scope
#     -> AP_DC_InvoiceHeader + AP_DC_GL_Details
#       -> BC purchaseInvoices + lines + dimensionSetLines
#     -> ERP_POST='Y'
#
# AP_POST_MODE defaults to 'draft': the invoice, its lines and its dimensions
# are created and ERP_POST is set, but a BC user reviews and posts. A non-PO
# invoice has no PO authorising the spend, so invoice approval is the only
# control in that flow.
# ---------------------------------------------------------------------------

@app.function_name(name="PostNonPoInvoices")
@app.timer_trigger(schedule=uat.POST_CRON, arg_name="timer",
                   run_on_startup=False)
def PostNonPoInvoices(timer: func.TimerRequest) -> None:
    sql = SqlSession()
    try:
        summary = invoice_poster.run_batch(sql.conn)
        if summary["picked"]:
            logging.info("Non-PO invoice run: %s", summary)
    except Exception:
        logging.exception("PostNonPoInvoices failed")
        raise
    finally:
        sql.close()


@app.route(route="ap/post-invoices", auth_level=func.AuthLevel.FUNCTION,
           methods=["POST"])
def PostNonPoInvoicesNow(req: func.HttpRequest) -> func.HttpResponse:
    """
    Manual trigger, for UAT.

      POST /api/ap/post-invoices                  every pending record
      POST /api/ap/post-invoices?recordId=12345   one record, ignoring
                                                  STATUS and ERP_POST
      POST /api/ap/post-invoices?limit=1          just the first pending one
    """
    record_id = req.params.get("recordId")
    limit = req.params.get("limit")

    sql = SqlSession()
    try:
        summary = invoice_poster.run_batch(
            sql.conn,
            limit=int(limit) if limit else None,
            record_id=int(record_id) if record_id else None,
        )
        return func.HttpResponse(json.dumps(summary, indent=2, default=str),
                                 status_code=200,
                                 mimetype="application/json")
    except Exception as e:
        logging.exception("PostNonPoInvoicesNow failed")
        return func.HttpResponse(
            json.dumps({"status": "error", "error": str(e)}, default=str),
            status_code=500, mimetype="application/json")
    finally:
        sql.close()


@app.route(route="ap/pending", auth_level=func.AuthLevel.FUNCTION,
           methods=["GET"])
def PendingInvoices(req: func.HttpRequest) -> func.HttpResponse:
    """Read-only: what the timer would pick up on its next run."""
    sql = SqlSession()
    try:
        rows = invoice_poster.pending_records(sql.conn)
        return func.HttpResponse(
            json.dumps({
                "clientId": uat.CLIENT_ID,
                "companyIds": uat.COMPANY_IDS,
                "status": uat.DC_STATUS,
                "erpPost": uat.DC_ERP_POST_PENDING,
                "postMode": uat.POST_MODE,
                "pending": rows,
            }, indent=2, default=str),
            status_code=200, mimetype="application/json")
    finally:
        sql.close()


# ---------------------------------------------------------------------------
# Backfill - re-read from BC and upsert into the ContinuFlow tables
#
# Direct loading means a dbo row is only as current as the last webhook that
# reached it. Use this after missed or dead-lettered events, or when a lookup
# value changed in BC (payment terms, item category) without the vendor / PO
# itself changing.
#
#   POST /api/cf/backfill?entity=vendors
#   POST /api/cf/backfill?entity=purchaseOrders&modifiedSince=2026-10-01
#   POST /api/cf/backfill?entity=locations&company=889
#
# entity: vendors | locations | accounts | purchaseOrders | purchaseReceipts
# The HTTP gateway cuts responses off after ~230s; use modifiedSince for the
# document entities, which also fetch every document's lines.
# ---------------------------------------------------------------------------

@app.route(route="cf/backfill", auth_level=func.AuthLevel.FUNCTION,
           methods=["POST"])
def CfBackfill(req: func.HttpRequest) -> func.HttpResponse:
    entity = req.params.get("entity")
    company = req.params.get("company")
    modified_since = req.params.get("modifiedSince")
    top = req.params.get("top")

    if entity not in cf_direct.BACKFILL_ENTITIES:
        return func.HttpResponse(
            json.dumps({"error": f"entity must be one of "
                                 f"{list(cf_direct.BACKFILL_ENTITIES)}"}),
            status_code=400, mimetype="application/json")

    sql = SqlSession()
    try:
        summary = cf_direct.backfill(
            sql.conn, entity,
            company_ids=[int(company)] if company else None,
            modified_since=modified_since,
            top=int(top) if top else None,
        )
        return func.HttpResponse(json.dumps(summary, indent=2, default=str),
                                 status_code=200,
                                 mimetype="application/json")
    except Exception as e:
        logging.exception("CfBackfill failed")
        return func.HttpResponse(
            json.dumps({"status": "error", "error": str(e)}, default=str),
            status_code=500, mimetype="application/json")
    finally:
        sql.close()
