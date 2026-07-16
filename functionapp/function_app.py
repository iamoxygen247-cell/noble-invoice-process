"""
function_app.py — Noble invoice decision engine (Azure Functions, Python v2 model).

One HTTP-triggered function. The Power Automate flow calls it after fetching the
SharePoint file; this function decides what should happen and records it on the
ledger, then returns the decision. It does NOT write to Dataverse and does NOT
write the review queue item -- those belong to the flow.

The policy bucket (municipal vs commercial) and all field rules live in
field_policy.py; the routing gates live in gates.py. This module is a thin
orchestrator: it runs gate A1, calls CU, delegates the decision to gates.evaluate,
persists the result, and returns it. It contains no bill-type branching.

Flow per request:
  1. Validate the request (sourceId, fieldThreshold range, transport: base64
     decodes and is non-empty, or a url). A bad request 400s here, BEFORE any
     ledger write, so it can never claim the A1 slot and block a corrected retry.
  2. Gate A1 (atomic): claim the SharePoint item id in InvoiceExtractProcessLog.
     Concurrency guard only -- an item processed before is re-claimed and fully
     re-processed (a corrected invoice may arrive as the same file again).
  3. Call Content Understanding with the document bytes (binary transport).
  4. gates.evaluate -> routingDecision, bill_type bucket, write values, ledger fields.
  5. Persist the raw CU result + decision JSON as diagnostics blobs (best-effort,
     never fails the request; see diagnostics.py).
  6. Write ledger Status=Extracted + decision/bucket/policy stamps + diagnostics
     blob paths. If this write fails, the response reports status=Received (the
     row's true state) plus ledgerWriteError; the decision payload is still returned.
  7. Return the decision JSON (writeValues included for the flow to consume).

There is NO duplicate-invoice verification anywhere in the pipeline (requirement),
and no re-upload dedup: the same SharePoint item posted again is re-processed in
full (each happy-path run leads the flow to create another Dynamics row). Gate A1
only stops two CONCURRENT invocations processing the same item at once.

Request body (JSON):
  {
    "sourceId":      "<SharePoint item UniqueId GUID>",   # required (A1 key / identity)
    "contentBase64": "<base64 of the PDF bytes>",          # required for binary transport
    "fileName":      "invoice1.pdf",                       # optional (content-type hint; municipal
                                                           #  invoice-number fallback, ext stripped)
    "sourceFileUrl": "https://.../invoice1.pdf",           # optional (stored on the ledger)
    "url":           "https://...blob...?sas",             # optional, ad-hoc test only
    "fieldThreshold": 0.73                                 # optional, overrides field_policy.THRESHOLD
  }
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import PurePath
from typing import Optional, Tuple

import azure.functions as func

import gates
import cu_client
import diagnostics
import ledger
import field_policy

app = func.FunctionApp()


def _json(status: int, payload: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False, default=str),
        status_code=status,
        mimetype="application/json",
    )


def _field_threshold(body: dict) -> float:
    """Resolve the critical-field threshold. Single source of truth is
    field_policy.THRESHOLD; an env var or request body may override it.

    A request value outside (0, 1] raises ValueError (the route returns 400):
    a nonsense threshold like 0 would silently auto-write every invoice. An
    unparseable request or env value falls back with a logged warning; an
    out-of-range env value is server config, so it also falls back rather than
    failing every request."""
    raw = body.get("fieldThreshold")
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
            logging.warning("fieldThreshold %r is not a number; using the configured default", raw)
        if value is not None:
            if not 0.0 < value <= 1.0:
                raise ValueError(f"fieldThreshold must be a number in (0, 1], got {raw!r}")
            return value
    env = os.getenv("FIELD_CONFIDENCE_THRESHOLD")
    if env:
        try:
            value = float(env)
            if 0.0 < value <= 1.0:
                return value
            logging.warning(
                "FIELD_CONFIDENCE_THRESHOLD=%s is outside (0, 1]; using field_policy.THRESHOLD", env
            )
        except ValueError:
            logging.warning(
                "FIELD_CONFIDENCE_THRESHOLD=%s is not a number; using field_policy.THRESHOLD", env
            )
    return field_policy.THRESHOLD


def _decode_content(body: dict) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Validate the transport inputs BEFORE any ledger write. Returns
    (content_bytes, url, error): exactly one of the first two is set on success;
    error is the message for a 400 response. Strict base64 (validate=True, after
    stripping whitespace so line-wrapped input still decodes) so corrupted input
    fails here as a 400 instead of reaching CU as garbage bytes and 502ing.
    """
    content_b64 = body.get("contentBase64")
    if content_b64 is not None and not isinstance(content_b64, str):
        return None, None, "contentBase64 must be a base64 string"
    if content_b64 and content_b64.strip():
        compact = re.sub(r"\s+", "", content_b64)
        try:
            content = base64.b64decode(compact, validate=True)
        except ValueError:  # binascii.Error is a ValueError
            return None, None, "contentBase64 is not valid base64"
        if not content:
            return None, None, "contentBase64 decodes to empty content"
        return content, None, None

    url = body.get("url")
    if isinstance(url, str) and url.strip():
        return None, url, None

    return None, None, "provide contentBase64 (binary transport) or url"


def _lease_seconds() -> int:
    try:
        return int(os.getenv("A1_LEASE_SECONDS", "600"))
    except ValueError:
        return 600


def _a1(table, source_id: str, received_fields: dict, lease_seconds: int):
    """
    Gate A1 — atomic concurrency claim (NOT re-upload dedup: that was removed so
    the same file can be uploaded again for re-processing). Returns the claimed
    row's etag (truthy str) to proceed, or a tuple ("skip", routingDecision,
    status, reason) to short-circuit (skip CU). The etag lets the caller later
    release ITS OWN claim on a graceful failure without ever touching a row a
    newer invocation has re-claimed.

    A row with a recorded decision no longer blocks: it is re-claimed and the
    item re-processed from scratch. The only skip left is a row another
    invocation is actively working on (undecided and within the lease). Both
    claim paths are atomic — insert for a first-seen item, etag-conditioned
    reset for a decided or stale row — so of two concurrent invocations for the
    same item exactly one proceeds, first upload or re-upload alike.
    """
    existing = ledger.get_row(table, source_id)
    if existing is None:
        etag = ledger.claim(table, source_id, Status="Received", **received_fields)
        if etag:
            return etag  # claimed atomically; Received already written
        existing = ledger.get_row(table, source_id)  # a concurrent run just claimed it

    reclaimable = ledger.has_decision(existing) or ledger.is_stale(existing, lease_seconds)
    if reclaimable:
        etag = ledger.reclaim(table, source_id, ledger.entity_etag(existing),
                              Status="Received", **received_fields)
        if etag:
            # decided -> a re-upload, re-process; stale Received -> a crashed run, resume.
            return etag

    return ("skip", "PROCESSING_IN_PROGRESS",
            (existing.get("Status") if existing else None) or "Received",
            "A1: another invocation is currently processing this SharePoint item")


@app.route(route="process-invoice", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def process_invoice(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _json(400, {"error": "request body must be JSON"})

    source_id = (body.get("sourceId") or "").strip()
    if not source_id:
        return _json(400, {"error": "sourceId is required (use the SharePoint item UniqueId)"})

    file_name = body.get("fileName") or ""
    sharepoint_url = body.get("sourceFileUrl") or ""

    # --- Validation: reject bad input BEFORE any ledger write ----------------
    # A request that 400s here must never claim the A1 slot; otherwise a
    # corrected retry would see PROCESSING_IN_PROGRESS until the lease expires.
    try:
        field_threshold = _field_threshold(body)
    except ValueError as exc:
        return _json(400, {"sourceId": source_id, "error": str(exc)})

    content, url, transport_error = _decode_content(body)
    if transport_error:
        return _json(400, {"sourceId": source_id, "error": transport_error})

    general_analyzer_id = cu_client.general_invoice_analyzer_id()

    try:
        table = ledger.get_table_client()
    except Exception as exc:
        logging.exception("Ledger client init failed")
        return _json(500, {"sourceId": source_id, "error": f"ledger init failed: {exc}"})

    pk, rk = ledger.keys_for_source_id(source_id)
    received_fields = {"FileName": file_name, "SharePointUrl": sharepoint_url,
                       "RoutingDecision": "", "FailedStage": "", "LastError": ""}

    # --- Gate A1: atomic concurrency claim ------------------------------------
    try:
        a1 = _a1(table, source_id, received_fields, _lease_seconds())
    except Exception as exc:
        logging.exception("Ledger A1 / Received write failed")
        return _json(500, {"sourceId": source_id, "partitionKey": pk, "rowKey": rk,
                           "error": f"ledger A1 write failed: {exc}"})

    if isinstance(a1, tuple):
        _, decision, status, reason = a1
        logging.info("A1 short-circuit for %s (decision=%s)", source_id, decision)
        return _json(200, {
            "sourceId": source_id, "partitionKey": pk, "rowKey": rk,
            "invoiceFileName": PurePath(file_name).stem,
            "alreadyProcessed": True, "skippedCU": True,
            "status": status, "routingDecision": decision,
            "reviewReasons": [reason],
        })
    claim_etag = a1

    # --- Content Understanding ----------------------------------------------
    # content/url were validated and decoded before the A1 claim, so an error
    # here is a genuine CU failure (502), never a bad request.
    cu_started = time.monotonic()
    try:
        if content is not None:
            full = cu_client.analyze_binary(content, file_name)
        else:
            full = cu_client.analyze_url(url)
    except Exception as exc:
        # Release the claim: this run is over, so mark the row Failed (making it
        # immediately reclaimable by the caller's automatic retry) instead of
        # holding the A1 lease and turning that retry into a silent
        # PROCESSING_IN_PROGRESS no-op. Conditioned on OUR claim's etag so a
        # delayed release can never clobber a newer invocation's claim; if the
        # etag lost (foreign owner) or the write fails, the row stays Received
        # and the lease fallback applies as before. Best-effort — the 502 is
        # returned either way.
        logging.exception("Content Understanding analyze failed")
        released = None
        try:
            released = ledger.reclaim(table, source_id, claim_etag, Status="Failed",
                                      FailedStage="cu_analyze", LastError=str(exc)[:1024])
            if not released:
                logging.warning("Failure release lost the etag race; row left as-is")
        except Exception:
            logging.exception("Ledger failure-release write failed")
        return _json(502, {"sourceId": source_id, "partitionKey": pk, "rowKey": rk,
                           "status": "Failed" if released else "Received",
                           "error": f"Content Understanding analyze failed: {exc}"})
    cu_duration_ms = int((time.monotonic() - cu_started) * 1000)

    try:
        # --- Gates / decision ------------------------------------------------
        result = gates.evaluate(full, field_threshold, general_analyzer_id, file_name=file_name)

        # --- Diagnostics sidecar: raw CU result + decision JSON (best-effort) -
        blob_paths = diagnostics.save_run(source_id, full, result)
        raw_blob, decision_blob = blob_paths if blob_paths else ("", "")

        # --- Ledger: Extracted + decision + bill-type/policy stamps -----------
        ledger_write_error: Optional[str] = None
        try:
            ledger.upsert(
                table, source_id,
                Status="Extracted",
                RoutingDecision=result["routingDecision"],
                DocumentType=result["effectiveDocumentType"],
                BillType=result.get("billType") or "",
                SubBillType=result.get("subBillType") or "",
                PolicyBucket=result.get("policyBucket") or "",
                PolicyVersion=result.get("policyVersion") or "",
                DefaultedFields=",".join(result.get("defaultedFields") or []),
                IsHandwritten=result.get("isHandwritten") or "",
                FileName=file_name,
                SharePointUrl=sharepoint_url,
                AnalyzerId=result.get("analyzerUsed") or "",
                CuDurationMs=cu_duration_ms,
                RawResultBlob=raw_blob,
                DecisionBlob=decision_blob,
                FailedStage="",
                LastError="",
            )
        except Exception as exc:
            logging.exception("Ledger Extracted write failed")
            ledger_write_error = str(exc)

        response = {
            "sourceId": source_id, "partitionKey": pk, "rowKey": rk,
            "invoiceFileName": PurePath(file_name).stem,
            "alreadyProcessed": False, "skippedCU": False,
            # status mirrors the ledger row's true state: if the Extracted write
            # failed the row is still at Received, and the flow must not treat the
            # decision as recorded.
            "status": "Extracted" if ledger_write_error is None else "Received",
        }
        response.update(result)  # includes writeValues for the Power Automate flow
        if ledger_write_error is not None:
            response["ledgerWriteError"] = ledger_write_error
        return _json(200, response)
    except Exception as exc:
        # Same release as the CU failure path: an unhandled decision-path bug
        # would otherwise 500 while still holding the A1 claim, and the flow's
        # automatic retry (which can arrive inside the lease) would get a 200
        # PROCESSING_IN_PROGRESS no-op and end the retry chain — a silent drop.
        # Releasing to Failed makes the retry re-claim and re-process at once.
        logging.exception("Decision path failed after CU")
        released = None
        try:
            released = ledger.reclaim(table, source_id, claim_etag, Status="Failed",
                                      FailedStage="decision", LastError=str(exc)[:1024])
            if not released:
                logging.warning("Failure release lost the etag race; row left as-is")
        except Exception:
            logging.exception("Ledger failure-release write failed")
        return _json(500, {"sourceId": source_id, "partitionKey": pk, "rowKey": rk,
                           "status": "Failed" if released else "Received",
                           "error": f"decision path failed: {exc}"})
