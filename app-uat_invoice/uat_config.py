"""
Settings for the UAT scope: BC -> ContinuFlow tables, and non-PO invoice
posting back to BC.

The database holds data for many clients and companies. Every statement in
this app is scoped by CLIENT_ID and COMPANY_ID so it can never read or write
another client's rows.
"""

import json
import os


def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Required app setting '{name}' is not set")
    return v


# ---------------------------------------------------------------------------
# Tenant scope - the single most important setting in this file
# ---------------------------------------------------------------------------

CLIENT_ID = int(_env("CF_CLIENT_ID", "363"))

# Companies in scope. 889 and 890 both belong to client 363.
COMPANY_IDS: list[int] = json.loads(_env("CF_COMPANY_IDS", "[889, 890]"))


def company_scope_sql(alias: str = "c") -> str:
    """
    Predicate restricting any statement to the configured client/company.
    Inlined rather than parameterised because it is built from ints this
    module validated, and it has to appear inside larger SQL templates.
    """
    ids = ",".join(str(int(i)) for i in COMPANY_IDS)
    return f"{alias}.CLIENT_ID = {int(CLIENT_ID)} AND {alias}.COMPANY_ID IN ({ids})"


# ---------------------------------------------------------------------------
# Business Central
# ---------------------------------------------------------------------------

API_HOST = "https://api.businesscentral.dynamics.com"
BC_ENVIRONMENT = _env("BC_ENVIRONMENT", "D365-BC-QA")
BC_API_BASE = f"{API_HOST}/v2.0/{BC_ENVIRONMENT}/api/v2.0"

# {company-guid: company-name} - the app's existing setting
BC_COMPANY_BY_GUID: dict[str, str] = json.loads(_env("BC_COMPANY_ID", "{}"))
# reverse, for posting
BC_GUID_BY_COMPANY: dict[str, str] = {
    name: guid for guid, name in BC_COMPANY_BY_GUID.items()
}

BC_HTTP_TIMEOUT = int(_env("BC_HTTP_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_SERVER = _env("SQL_SERVER", required=True)
SQL_DATABASE = _env("SQL_DATABASE", required=True)
SQL_USER = _env("SQL_USER", required=True)
SQL_PASSWORD = _env("SQL_PASSWORD", required=True)


# ---------------------------------------------------------------------------
# Direct load (BC -> ContinuFlow tables)
# ---------------------------------------------------------------------------

# Vendors, locations, accounts, purchase orders (+ lines) and purchase
# receipts (+ lines) are written straight into the dbo tables from the CF
# doc, with no bc.stg_* copy. paymentTerms, paymentMethods and items still
# land in bc.stg_* because the mappings look values up from them.
#
# Rows are updated in place and only inserted when new, so IDENTITY ids
# (Mst_Store.PROFIT_CENTER_ID in particular) never change, and only columns
# sourced from BC are overwritten - anything the CF team fills in the other
# columns is left alone.


def _company_map() -> dict[str, str]:
    # {"889": "<bc-company-guid>", "890": "<bc-company-guid>"}
    return json.loads(_env("CF_BC_COMPANY_MAP", "{}"))


def cf_company_id(bc_company_guid: str) -> int | None:
    """BC company GUID -> ContinuFlow COMPANY_ID, or None if not mapped."""
    for cid, guid in _company_map().items():
        if str(guid).lower() == str(bc_company_guid).lower():
            return int(cid)
    return None


# ---------------------------------------------------------------------------
# Non-PO invoice posting (ContinuFlow tables -> BC)
# ---------------------------------------------------------------------------

# Which AP_DataConversions rows to pick up
DC_STATUS = _env("AP_DC_STATUS", "APPROVED")
DC_ERP_POST_PENDING = _env("AP_DC_ERP_POST_PENDING", "N")
DC_ERP_POST_DONE = _env("AP_DC_ERP_POST_DONE", "Y")
# Written to ERP_POST once MAX_ATTEMPTS is exhausted, so the timer stops
# retrying a record that will never succeed.
DC_ERP_POST_FAILED = _env("AP_DC_ERP_POST_FAILED", "E")

MAX_ATTEMPTS = int(_env("AP_POST_MAX_ATTEMPTS", "3"))
BATCH_SIZE = int(_env("AP_POST_BATCH_SIZE", "10"))

POST_CRON = _env("AP_POST_CRON", "0 */5 * * * *")      # every 5 minutes

# draft - create the invoice, its lines and its dimensions, do NOT post it.
#         ERP_POST is still set to 'Y'. A BC user reviews and posts.
# post  - as above, then call Microsoft.NAV.post.
#
# Default is draft. A non-PO invoice has no PO authorising the spend, so
# invoice approval is the only control in that flow, and a posted document in
# BC is immutable.
POST_MODE = _env("AP_POST_MODE", "draft").lower()

# unitCost is sent in the line POST. Set true to also PATCH it afterwards,
# in case BC overwrites it from the account's defaults.
UNITCOST_PATCH = _env("AP_UNITCOST_PATCH", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

# Posted to purchaseInvoiceLines({id})/dimensionSetLines after each line.
COST_CENTER_DIMENSION_CODE = _env("BC_COST_CENTER_DIM_CODE", "COST CENTER")
DIVISION_DIMENSION_CODE = _env("BC_DIVISION_DIM_CODE", "DIVISION")

POST_COST_CENTER = _env("AP_POST_COST_CENTER", "true").lower() == "true"

# DIVISION = mst_store.PROFIT_CENTER_CODE for the GL line's PROFIT_CENTER_ID
# (see load_gl_lines / division_value in invoice_poster.py).
POST_DIVISION = _env("AP_POST_DIVISION", "true").lower() == "true"

# A dimension value BC does not recognise fails the whole line. When this is
# true a rejected dimension is logged and the line is kept; when false the
# invoice is held. True for UAT so one bad cost centre does not block a run.
DIMENSION_FAILURE_IS_FATAL = _env(
    "AP_DIMENSION_FAILURE_FATAL", "false").lower() == "true"


def bc_company_guid(company_id: int) -> str:
    """
    Map a ContinuFlow COMPANY_ID to a BC company GUID.

    CF_BC_COMPANY_MAP is {"889": "<bc-company-guid>", "890": "..."} and is
    required both ways - the DB knows COMPANY_ID, BC knows its own GUID, and
    nothing joins them automatically.
    """
    guid = _company_map().get(str(company_id))
    if not guid:
        raise ValueError(
            f"No CF_BC_COMPANY_MAP entry for COMPANY_ID {company_id}. "
            f"Set it to {{\"889\": \"<bc-guid>\", \"890\": \"<bc-guid>\"}}."
        )
    return guid
