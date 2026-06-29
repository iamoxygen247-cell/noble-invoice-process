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
  1. Gate A1 (atomic): claim the SharePoint item id in InvoiceExtractProcessLog.
  2. Call Content Understanding with the document bytes (binary transport).
  3. gates.evaluate -> routingDecision, bill_type bucket, write values, ledger fields.
  4. Write ledger Status=Extracted + decision/bucket/policy stamps.
  5. Return the decision JSON (writeValues included for the flow to consume).

There is NO duplicate-invoice verification anywhere in the pipeline (requirement).
Gate A1 is ingestion idempotency only (same SharePoint upload not processed twice).

Request body (JSON):
  {
    "sourceId":      "<SharePoint item UniqueId GUID>",   # required (A1 key / identity)
    "contentBase64": "<base64 of the PDF bytes>",          # required for binary transport
    "fileName":      "invoice1.pdf",                       # optional (content-type hint)
    "sourceFileUrl": "https://.../invoice1.pdf",           # optional (stored on the ledger)
    "url":           "https://...blob...?sas",             # optional, ad-hoc test only
    "reprocess":     false,                                # optional, bypass gate A1
    "fieldThreshold": 0.73                                 # optional, overrides field_policy.THRESHOLD
  }
"""

from __future__ import annotations

import base64
import json
import logging
import os

import azure.functions as func

import gates
import cu_client
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
    field_policy.THRESHOLD; an env var or request body may override it."""
    if body.get("fieldThreshold") is not None:
        try:
            return float(body["fieldThreshold"])
        except (TypeError, ValueError):
            pass
    env = os.getenv("FIELD_CONFIDENCE_THRESHOLD")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return field_policy.THRESHOLD


def _lease_seconds() -> int:
    try:
        return int(os.getenv("A1_LEASE_SECONDS", "600"))
    except ValueError:
        return 600


def _a1(table, source_id: str, reprocess: bool, received_fields: dict, lease_seconds: int):
    """
    Gate A1 — atomic idempotency claim. Returns None to proceed, or a tuple
    ("skip", routingDecision, status, reason) to short-circuit (skip CU).
    """
    if reprocess:
        ledger.upsert(table, source_id, Status="Received", **received_fields)
        return None

    existing = ledger.get_row(table, source_id)
    if existing is None:
        if ledger.claim(table, source_id, Status="Received", **received_fields):
            return None  # claimed atomically; Received already written
        existing = ledger.get_row(table, source_id)  # a concurrent run just claimed it

    if ledger.has_decision(existing):
        return ("skip",
                (existing.get("RoutingDecision") or "ALREADY_PROCESSED"),
                existing.get("Status"),
                "A1: SharePoint item id already processed; not reprocessed")

    if ledger.is_stale(existing, lease_seconds):
        ledger.upsert(table, source_id, Status="Received", **received_fields)  # resume crashed run
        return None

    return ("skip", "PROCESSING_IN_PROGRESS", existing.get("Status") if existing else "Received",
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
    reprocess = bool(body.get("reprocess", False))
    content_b64 = body.get("contentBase64")
    url = body.get("url")
    field_threshold = _field_threshold(body)
    general_analyzer_id = cu_client.general_invoice_analyzer_id()

    try:
        table = ledger.get_table_client()
    except Exception as exc:
        logging.exception("Ledger client init failed")
        return _json(500, {"sourceId": source_id, "error": f"ledger init failed: {exc}"})

    pk, rk = ledger.keys_for_source_id(source_id)
    received_fields = {"FileName": file_name, "SharePointUrl": sharepoint_url, "RoutingDecision": ""}

    # --- Gate A1: atomic idempotency claim -----------------------------------
    try:
        a1 = _a1(table, source_id, reprocess, received_fields, _lease_seconds())
    except Exception as exc:
        logging.exception("Ledger A1 / Received write failed")
        return _json(500, {"sourceId": source_id, "partitionKey": pk, "rowKey": rk,
                           "error": f"ledger A1 write failed: {exc}"})

    if a1 is not None:
        _, decision, status, reason = a1
        logging.info("A1 short-circuit for %s (decision=%s)", source_id, decision)
        return _json(200, {
            "sourceId": source_id, "partitionKey": pk, "rowKey": rk,
            "alreadyProcessed": True, "skippedCU": True,
            "status": status, "routingDecision": decision,
            "reviewReasons": [reason],
        })

    # --- Content Understanding ----------------------------------------------
    try:
        if content_b64:
            full = cu_client.analyze_binary(base64.b64decode(content_b64), file_name)
        elif url:
            full = cu_client.analyze_url(url)
        else:
            return _json(400, {"sourceId": source_id,
                               "error": "provide contentBase64 (binary transport) or url"})
    except Exception as exc:
        # Leave the row at Received: it shows as in-flight/stuck and will be
        # resumed on the next trigger once it is older than the A1 lease.
        logging.exception("Content Understanding analyze failed")
        return _json(502, {"sourceId": source_id, "partitionKey": pk, "rowKey": rk,
                           "status": "Received",
                           "error": f"Content Understanding analyze failed: {exc}"})

    # --- Gates / decision ----------------------------------------------------
    result = gates.evaluate(full, field_threshold, general_analyzer_id)

    # --- Ledger: Extracted + decision + bill-type/policy stamps ---------------
    try:
        ledger.upsert(
            table, source_id,
            Status="Extracted",
            RoutingDecision=result["routingDecision"],
            DocumentType=result["effectiveDocumentType"],
            BillType=result.get("billType") or "",
            PolicyBucket=result.get("policyBucket") or "",
            PolicyVersion=result.get("policyVersion") or "",
            DefaultedFields=",".join(result.get("defaultedFields") or []),
            IsHandwritten=result.get("isHandwritten") or "",
            FileName=file_name,
            SharePointUrl=sharepoint_url,
        )
    except Exception as exc:
        logging.exception("Ledger Extracted write failed")
        result["ledgerWriteError"] = str(exc)

    response = {
        "sourceId": source_id, "partitionKey": pk, "rowKey": rk,
        "alreadyProcessed": False, "skippedCU": False, "status": "Extracted",
    }
    response.update(result)  # includes writeValues for the Power Automate flow
    return _json(200, response)
