"""
diagnostics.py — per-run diagnostic sidecar (Azure Blob Storage) for the Function.

The ledger row holds only the routing decision and policy stamps; the evidence
needed to diagnose a bad extraction (the raw Content Understanding result with
per-field confidences, and the decision JSON returned to the flow) does not fit
Table Storage's limits (64 KB per property, 1 MB per entity). This module
persists both as blobs and the route stamps the blob paths on the ledger row.

Per run, two blobs under the row's RowKey prefix (timestamped, so re-uploads of
the same SharePoint item keep a run history):
  {rk}/{yyyymmddThhmmssZ}-raw.json       full cu_client analyze result
  {rk}/{yyyymmddThhmmssZ}-decision.json  gates.evaluate result
plus, only on a B2 rescue, a third:
  {rk}/{yyyymmddThhmmssZ}-raw-rescue.json  the general-invoice re-analysis

Best-effort by contract: save_run catches every exception, logs a warning, and
returns None — a diagnostics outage must never fail invoice processing.

Retention is a lifecycle-management rule on the container (delete after 90 days,
matching ledger.DEFAULT_RETENTION_DAYS); the Function does not delete blobs.

Config (environment variables):
  DIAGNOSTICS_CONTAINER          container name (default invoice-diagnostics)
  AZURE_STORAGE_ACCOUNT          storage account name (builds the blob endpoint)
  AZURE_TABLES_CONNECTION_STRING optional; a full storage connection string
                                 (covers blobs too — Azurite locally); if set,
                                 used instead of AAD
  AZURE_CLIENT_ID                optional; disambiguates the managed identity
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import ledger

DEFAULT_CONTAINER_NAME = "invoice-diagnostics"
BLOB_ENDPOINT_TEMPLATE = "https://{account}.blob.core.windows.net"


def container_name() -> str:
    return os.getenv("DIAGNOSTICS_CONTAINER") or DEFAULT_CONTAINER_NAME


def get_container_client():
    """Return a container client, creating the container if needed. Connection
    string takes precedence if provided; otherwise DefaultAzureCredential is
    used (same precedence as ledger.get_table_client)."""
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import BlobServiceClient

    conn = os.getenv("AZURE_TABLES_CONNECTION_STRING")
    if conn:
        service = BlobServiceClient.from_connection_string(conn)
    else:
        account = os.getenv("AZURE_STORAGE_ACCOUNT")
        if not account:
            raise RuntimeError(
                "Set AZURE_STORAGE_ACCOUNT (storage account name) or "
                "AZURE_TABLES_CONNECTION_STRING."
            )
        from azure.identity import DefaultAzureCredential

        endpoint = BLOB_ENDPOINT_TEMPLATE.format(account=account)
        service = BlobServiceClient(account_url=endpoint, credential=DefaultAzureCredential())

    container = service.get_container_client(container_name())
    try:
        container.create_container()
    except ResourceExistsError:
        pass
    return container


def _dump(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def save_run(
    source_id: str,
    raw_full: Dict[str, Any],
    decision: Dict[str, Any],
    rescue_raw: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    """
    Persist one run's raw CU result and decision JSON. Returns
    (raw_blob_path, decision_blob_path), or None if anything failed — every
    exception is swallowed and logged so diagnostics can never fail the request.

    ``rescue_raw`` is the second CU result from a B2 rescue, written alongside as
    ``-raw-rescue.json``. ``-raw.json`` stays the router response so its meaning is
    unchanged for every run; note that replaying it reproduces the *pre-rescue*
    reject, and the rescue blob is the one behind the decision that was returned.
    """
    try:
        container = get_container_client()
        _, rk = ledger.keys_for_source_id(source_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_path = f"{rk}/{stamp}-raw.json"
        decision_path = f"{rk}/{stamp}-decision.json"
        # overwrite=True: two runs of the same item within one second collide
        # on the timestamp; the later run's artifacts win rather than erroring.
        container.upload_blob(raw_path, _dump(raw_full), overwrite=True)
        container.upload_blob(decision_path, _dump(decision), overwrite=True)
        if rescue_raw is not None:
            container.upload_blob(f"{rk}/{stamp}-raw-rescue.json",
                                  _dump(rescue_raw), overwrite=True)
        return raw_path, decision_path
    except Exception:
        logging.warning("Diagnostics blob write failed (processing continues)", exc_info=True)
        return None
