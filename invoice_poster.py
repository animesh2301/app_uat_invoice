"""
ContinuFlow tables -> Business Central: non-PO invoice posting.

    AP_DataConversions   CLIENT_ID / COMPANY_ID in scope,
                         STATUS = 'APPROVED', ERP_POST = 'N'
        -> AP_DC_InvoiceHeader   (one header per RECORD_ID)
        -> AP_DC_GL_Details      (every row is an invoice line;
                                  COST_CENTER and DIVISION are dimensions)
            -> BC purchaseInvoices + purchaseInvoiceLines
               + dimensionSetLines for COST CENTER / DIVISION
        -> ERP_POST = 'Y'

Non-PO only. There is no PO and no receipt, so nothing is missing: the
standard API models this document completely.

Retries are safe. Every BC identifier is written to bc.InvoicePostLog the
moment it exists, so a retry resumes the half-built invoice instead of
creating a second one.
"""

import logging
import re
from decimal import Decimal

import uat_config as cfg
from bc_http import BcError, bc_get, bc_request

log = logging.getLogger("invoice_poster")

ZERO = Decimal("0")
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class Held(Exception):
    """Needs a person. Not retried - ERP_POST is left pending and flagged."""


def _dec(v, default=ZERO) -> Decimal:
    if v is None or v == "":
        return default
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _rows(cursor):
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def pending_records(conn, limit=None) -> list[dict]:
    limit = limit or cfg.BATCH_SIZE
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT TOP (%s) dc.RECORD_ID, dc.CLIENT_ID, dc.COMPANY_ID,
                        dc.DOC_NO, dc.INVOICE_TYPE, dc.STATUS, dc.ERP_POST
        FROM dbo.AP_DataConversions dc
        WHERE {cfg.company_scope_sql('dc')}
          AND dc.STATUS   = %s
          AND dc.ERP_POST = %s
        ORDER BY dc.RECORD_ID
        """,
        (limit, cfg.DC_STATUS, cfg.DC_ERP_POST_PENDING),
    )
    return _rows(cur)


def load_header(conn, record_id) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1 *
        FROM dbo.AP_DC_InvoiceHeader
        WHERE RECORD_ID = %s
        ORDER BY ID DESC
        """,
        (record_id,),
    )
    rows = _rows(cur)
    return rows[0] if rows else None


def load_gl_lines(conn, record_id) -> list[dict]:
    """
    Every AP_DC_GL_Details row for the record becomes one invoice line - no
    filtering. COST_CENTER and DIVISION are read from the same row.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM dbo.AP_DC_GL_Details
        WHERE RECORD_ID = %s
        ORDER BY SERIAL_NO, ID
        """,
        (record_id,),
    )
    return _rows(cur)


def resolve_account(conn, client_id, company_id, gl_code, bc_company_guid):
    """
    GLCODE (the account number, e.g. 61000) -> BC account id.

    COA_Master_SAP.ACCOUNT_NUMBER holds the BC account id. The same account
    number exists in both companies with a different id in each, so the
    lookup is always filtered on this invoice's CLIENT_ID and COMPANY_ID -
    never on the code alone.

    If COA_Master_SAP has no usable row, BC is asked directly, again in this
    invoice's company. Two different ids for one code in the same company is
    ambiguous and holds the invoice rather than guessing.
    """
    code = str(gl_code or "").strip()
    if not code:
        return None, "no GLCODE on the line"

    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT ACCOUNT_NUMBER
        FROM dbo.COA_Master_SAP
        WHERE CLIENT_ID = %s AND COMPANY_ID = %s AND ACCOUNT_CODE = %s
          AND ACCOUNT_NUMBER IS NOT NULL
        """,
        (client_id, company_id, code),
    )
    ids = {str(r[0]).strip() for r in cur.fetchall()
           if r[0] and GUID_RE.match(str(r[0]).strip())}

    if len(ids) == 1:
        return ids.pop(), None
    if len(ids) > 1:
        return None, (f"G/L {code} has {len(ids)} different BC ids in "
                      f"COA_Master_SAP for company {company_id}: "
                      f"{sorted(ids)}")

    url = (f"{cfg.BC_API_BASE}/companies({bc_company_guid})/accounts"
           f"?$filter=number eq '{code.replace(chr(39), chr(39) * 2)}'")
    vals = (bc_get(url, timeout=cfg.BC_HTTP_TIMEOUT) or {}).get("value", [])
    if vals:
        return vals[0]["id"], None

    return None, (f"G/L account {code} not found for company {company_id} "
                  f"in COA_Master_SAP or in BC")


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

def cost_center_value(header: dict, line: dict):
    v = line.get("COST_CENTER")
    v = str(v).strip() if v is not None else ""
    return v or None


def division_value(header: dict, line: dict):
    v = line.get("DIVISION")
    v = str(v).strip() if v is not None else ""
    return v or None


def post_dimension(bc_company_guid, invoice_id, line_id, code, value_code):
    url = (f"{cfg.BC_API_BASE}/companies({bc_company_guid})"
           f"/purchaseInvoices({invoice_id})"
           f"/purchaseInvoiceLines({line_id})/dimensionSetLines")
    return bc_request("POST", url,
                      json_body={"code": code, "valueCode": str(value_code)},
                      timeout=cfg.BC_HTTP_TIMEOUT)


# ---------------------------------------------------------------------------
# Logging / write-back
# ---------------------------------------------------------------------------

def log_step(conn, record_id, attempt, step, outcome, detail=None,
             bc_invoice_id=None, bc_invoice_number=None, client_id=None,
             company_id=None) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = str(detail)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bc.InvoicePostLog
            (RecordId, ClientId, CompanyId, Attempt, Step, Outcome, Detail,
             BcInvoiceId, BcInvoiceNumber)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (record_id, client_id, company_id, attempt, step, outcome,
         (detail or "")[:4000], bc_invoice_id, bc_invoice_number),
    )
    conn.commit()


def attempt_number(conn, record_id) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT ISNULL(MAX(Attempt), 0) + 1 FROM bc.InvoicePostLog "
        "WHERE RecordId = %s",
        (record_id,),
    )
    return int(cur.fetchone()[0])


def resume_invoice_id(conn, record_id) -> str | None:
    """
    An invoice this record already created in a previous attempt. Without
    this, a retry after a mid-way failure would create a second invoice in BC.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1 BcInvoiceId
        FROM bc.InvoicePostLog
        WHERE RecordId = %s AND Step = 'create_header' AND Outcome = 'ok'
              AND BcInvoiceId IS NOT NULL
        ORDER BY LogId DESC
        """,
        (record_id,),
    )
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def mark_posted(conn, record_id) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.AP_DataConversions
           SET ERP_POST = %s, ERP_ERROR = NULL
         WHERE RECORD_ID = %s
        """,
        (cfg.DC_ERP_POST_DONE, record_id),
    )
    conn.commit()


def mark_error(conn, record_id, message, attempt) -> None:
    """
    Latest error to ERP_ERROR so it shows in the ContinuFlow UI; full history
    stays in bc.InvoicePostLog. ERP_POST goes to the failed marker once the
    attempt cap is reached so the timer stops retrying.
    """
    exhausted = attempt >= cfg.MAX_ATTEMPTS
    new_state = cfg.DC_ERP_POST_FAILED if exhausted \
        else cfg.DC_ERP_POST_PENDING

    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.AP_DataConversions
           SET ERP_ERROR = %s, ERP_POST = %s
         WHERE RECORD_ID = %s
        """,
        (f"[attempt {attempt}] {message}"[:4000], new_state, record_id),
    )
    conn.commit()
    return exhausted


# ---------------------------------------------------------------------------
# One record
# ---------------------------------------------------------------------------

def process_record(conn, dc: dict) -> dict:
    record_id = dc["RECORD_ID"]
    client_id = dc["CLIENT_ID"]
    company_id = dc["COMPANY_ID"]

    attempt = attempt_number(conn, record_id)

    def step(name, outcome, detail=None, inv_id=None, inv_no=None):
        log_step(conn, record_id, attempt, name, outcome, detail,
                 inv_id, inv_no, client_id, company_id)

    try:
        bc_guid = cfg.bc_company_guid(company_id)

        header = load_header(conn, record_id)
        if header is None:
            raise Held(f"no AP_DC_InvoiceHeader row for RECORD_ID {record_id}")

        lines = load_gl_lines(conn, record_id)
        step("load", "ok",
             f"header ID {header.get('ID')}, {len(lines)} line(s) to post")

        if not lines:
            raise Held("no AP_DC_GL_Details rows for this record")

        # ---- resolve every account before creating anything -------------
        resolved = []
        for l in lines:
            account_id, err = resolve_account(
                conn, client_id, company_id, l.get("GLCODE"), bc_guid)
            if err:
                raise Held(f"line SERIAL_NO {l.get('SERIAL_NO')}: {err}")
            resolved.append((l, account_id))
        step("resolve_accounts", "ok", f"{len(resolved)} account(s)")

        # ---- header (resume if a previous attempt made one) -------------
        invoice_id = resume_invoice_id(conn, record_id)
        if invoice_id:
            step("create_header", "skipped",
                 f"resuming invoice {invoice_id} from an earlier attempt",
                 inv_id=invoice_id)
        else:
            body = {"vendorNumber": str(header.get("VEND_CODE") or "").strip()}
            if header.get("INVOICE_NUMBER"):
                body["vendorInvoiceNumber"] = str(header["INVOICE_NUMBER"])
            if header.get("INVOICE_DATE"):
                body["invoiceDate"] = str(header["INVOICE_DATE"])[:10]
            if header.get("POSTING_DATE"):
                body["postingDate"] = str(header["POSTING_DATE"])[:10]
            if header.get("CURRENCY"):
                body["currencyCode"] = str(header["CURRENCY"]).strip()

            if not body["vendorNumber"]:
                raise Held("VEND_CODE is empty on AP_DC_InvoiceHeader")

            created = bc_request(
                "POST",
                f"{cfg.BC_API_BASE}/companies({bc_guid})/purchaseInvoices",
                json_body=body, timeout=cfg.BC_HTTP_TIMEOUT)
            invoice_id = created["id"]
            step("create_header", "ok", str(body),
                 inv_id=invoice_id, inv_no=created.get("number"))

        # ---- lines ------------------------------------------------------
        existing = (bc_get(
            f"{cfg.BC_API_BASE}/companies({bc_guid})"
            f"/purchaseInvoices({invoice_id})/purchaseInvoiceLines",
            timeout=cfg.BC_HTTP_TIMEOUT) or {}).get("value", [])

        for idx, (l, account_id) in enumerate(resolved):
            if idx < len(existing):
                continue        # created by an earlier attempt

            amount = _dec(l.get("AMOUNT"))
            description = (l.get("DESCRIPTION") or l.get("GL_DESCR") or "")
            description = str(description)[:100]

            line_body = {
                "lineType": "Account",
                "accountId": account_id,
                "description": description,
                "quantity": 1,
                "unitCost": float(amount),
            }
            new_line = bc_request(
                "POST",
                f"{cfg.BC_API_BASE}/companies({bc_guid})"
                f"/purchaseInvoices({invoice_id})/purchaseInvoiceLines",
                json_body=line_body,
                timeout=cfg.BC_HTTP_TIMEOUT)
            stored = (new_line or {}).get("unitCost")

            # Fallback, off by default: if BC's field validation ever
            # overwrites unitCost from the account again, set
            # AP_UNITCOST_PATCH=true to re-apply it with a second call.
            if cfg.UNITCOST_PATCH:
                patched = bc_request(
                    "PATCH",
                    f"{cfg.BC_API_BASE}/companies({bc_guid})"
                    f"/purchaseInvoiceLines({new_line['id']})",
                    json_body={"unitCost": float(amount)}, etag="*",
                    timeout=cfg.BC_HTTP_TIMEOUT)
                stored = (patched or {}).get("unitCost")

            step(f"line_{l.get('SERIAL_NO') or idx + 1}", "ok",
                 f"GL {l.get('GLCODE')} requested {amount} stored {stored}")

            # ---- dimensions ---------------------------------------------
            for enabled, code, value in (
                (cfg.POST_COST_CENTER, cfg.COST_CENTER_DIMENSION_CODE,
                 cost_center_value(header, l)),
                (cfg.POST_DIVISION, cfg.DIVISION_DIMENSION_CODE,
                 division_value(header, l)),
            ):
                if not enabled:
                    continue
                if not value:
                    step(f"dim_{code}", "skipped", "no value on the line")
                    continue
                try:
                    post_dimension(bc_guid, invoice_id, new_line["id"],
                                   code, value)
                    step(f"dim_{code}", "ok", f"{code} = {value}")
                except BcError as e:
                    step(f"dim_{code}", "error",
                         f"{code} = {value}: {e.body or e}")
                    if cfg.DIMENSION_FAILURE_IS_FATAL:
                        raise Held(
                            f"dimension {code} = {value} rejected by BC: "
                            f"{e.body or e}")

        invoice = bc_get(f"{cfg.BC_API_BASE}/companies({bc_guid})"
                         f"/purchaseInvoices({invoice_id})",
                         timeout=cfg.BC_HTTP_TIMEOUT) or {}

        # ---- post, or leave as a draft ----------------------------------
        if cfg.POST_MODE == "post":
            bc_request("POST",
                       f"{cfg.BC_API_BASE}/companies({bc_guid})"
                       f"/purchaseInvoices({invoice_id})/Microsoft.NAV.post",
                       timeout=cfg.BC_HTTP_TIMEOUT)
            step("post_invoice", "ok", invoice.get("number"),
                 inv_id=invoice_id, inv_no=invoice.get("number"))
            outcome = "posted"
        else:
            step("post_invoice", "skipped",
                 "AP_POST_MODE=draft - a BC user reviews and posts",
                 inv_id=invoice_id, inv_no=invoice.get("number"))
            outcome = "draft_created"

        mark_posted(conn, record_id)
        step("erp_post", "ok", f"ERP_POST set to {cfg.DC_ERP_POST_DONE}")

        return {
            "recordId": record_id,
            "status": outcome,
            "invoiceId": invoice_id,
            "invoiceNumber": invoice.get("number"),
            "lines": len(resolved),
            "totalAmountExcludingTax": invoice.get("totalAmountExcludingTax"),
        }

    except Held as h:
        step("held", "held", str(h))
        exhausted = mark_error(conn, record_id, f"HELD: {h}", attempt)
        log.warning("Record %s held: %s", record_id, h)
        return {"recordId": record_id, "status": "held", "reason": str(h),
                "attempt": attempt, "retriesExhausted": exhausted}

    except BcError as e:
        detail = f"{e} :: {e.body}"
        step("bc_error", "error", detail)
        exhausted = mark_error(conn, record_id, detail, attempt)
        log.exception("Record %s BC error", record_id)
        return {"recordId": record_id, "status": "bc_error", "error": str(e),
                "bcResponse": e.body, "attempt": attempt,
                "retriesExhausted": exhausted}

    except Exception as e:
        step("error", "error", str(e))
        exhausted = mark_error(conn, record_id, str(e), attempt)
        log.exception("Record %s failed", record_id)
        return {"recordId": record_id, "status": "error", "error": str(e),
                "attempt": attempt, "retriesExhausted": exhausted}


def run_batch(conn, limit=None, record_id=None) -> dict:
    """
    record_id given -> just that one, ignoring STATUS/ERP_POST, for UAT.
    Otherwise everything pending, up to the batch size.
    """
    if record_id is not None:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT dc.RECORD_ID, dc.CLIENT_ID, dc.COMPANY_ID, dc.DOC_NO,
                   dc.INVOICE_TYPE, dc.STATUS, dc.ERP_POST
            FROM dbo.AP_DataConversions dc
            WHERE dc.RECORD_ID = %s AND {cfg.company_scope_sql('dc')}
            """,
            (record_id,),
        )
        records = _rows(cur)
        if not records:
            return {"picked": 0,
                    "note": f"RECORD_ID {record_id} not found within "
                            f"CLIENT_ID {cfg.CLIENT_ID} / companies "
                            f"{cfg.COMPANY_IDS}",
                    "results": []}
    else:
        records = pending_records(conn, limit)

    results = [process_record(conn, dc) for dc in records]

    return {
        "picked": len(records),
        "mode": cfg.POST_MODE,
        "results": results,
    }
