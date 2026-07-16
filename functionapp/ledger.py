"""
ledger.py — InvoiceExtractProcessLog (Azure Table Storage) client for the Function.

Lifted from the validated Phase 3 harness (phase3_ledger_test.py):
  - row identity is the SharePoint item UniqueId (gate A1 concurrency key)
  - RowKey = sanitized item id; PartitionKey = a short prefix of it
  - upsert is idempotent: re-running the same source id updates in place,
    preserves the original IngestedUtc, and never blanks a recorded
    DynamicsRecordId
  - auth: DefaultAzureCredential (managed identity in Azure, az login locally),
    with an AZURE_TABLES_CONNECTION_STRING fallback for quick local runs

The Function writes Received then Extracted+decision. The terminal
Written + DynamicsRecordId is written by the Logic App after Dataverse returns
201 — the Function does not write it (single Dataverse-write owner).

Config (environment variables):
  AZURE_STORAGE_ACCOUNT          storage account name (builds the table endpoint)
  AZURE_TABLES_TABLE             table name (default InvoiceExtractProcessLog)
  AZURE_TABLES_CONNECTION_STRING optional; if set, used instead of AAD
  AZURE_CLIENT_ID                optional; disambiguates the managed identity
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

DEFAULT_TABLE_NAME = "InvoiceExtractProcessLog"
DEFAULT_RETENTION_DAYS = 90  # prototype; production matches AP audit policy (min 13 months)
TABLES_ENDPOINT_TEMPLATE = "https://{account}.table.core.windows.net"
PARTITION_PREFIX_LEN = 2

# Table Storage forbids these in PartitionKey/RowKey: / \ # ? and control characters.
_INVALID_KEY_CHARS = re.compile(r"[\u0000-\u001F\u007F-\u009F/\\#?]")


def table_name() -> str:
    return os.getenv("AZURE_TABLES_TABLE") or DEFAULT_TABLE_NAME


def sanitize_key(value: str) -> str:
    cleaned = value.strip().strip("{}")
    return _INVALID_KEY_CHARS.sub("-", cleaned)


def keys_for_source_id(source_id: str) -> Tuple[str, str]:
    rk = sanitize_key(source_id)
    pk = rk[:PARTITION_PREFIX_LEN] if rk else "00"
    return pk, rk


def get_table_client():
    """Return a table client, creating the table if needed. Connection string
    takes precedence if provided; otherwise DefaultAzureCredential is used."""
    from azure.data.tables import TableServiceClient

    conn = os.getenv("AZURE_TABLES_CONNECTION_STRING")
    if conn:
        service = TableServiceClient.from_connection_string(conn)
    else:
        account = os.getenv("AZURE_STORAGE_ACCOUNT")
        if not account:
            raise RuntimeError(
                "Set AZURE_STORAGE_ACCOUNT (storage account name) or "
                "AZURE_TABLES_CONNECTION_STRING."
            )
        from azure.identity import DefaultAzureCredential

        endpoint = TABLES_ENDPOINT_TEMPLATE.format(account=account)
        service = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())

    name = table_name()
    service.create_table_if_not_exists(name)
    return service.get_table_client(name)


def get_row(table, source_id: str) -> Optional[Dict[str, Any]]:
    """Point read by source id. Returns the entity or None (gate A1's check)."""
    from azure.core.exceptions import ResourceNotFoundError

    pk, rk = keys_for_source_id(source_id)
    try:
        return table.get_entity(partition_key=pk, row_key=rk)
    except ResourceNotFoundError:
        return None


def has_decision(entity: Optional[Dict[str, Any]]) -> bool:
    """True if a row already reached a routing decision (i.e. CU ran and gates
    were applied). A row at Received with no RoutingDecision is an incomplete
    prior attempt and should be resumed, not skipped."""
    if not entity:
        return False
    status = (entity.get("Status") or "").strip()
    decision = (entity.get("RoutingDecision") or "").strip()
    if decision:
        return True
    return status not in ("", "Received")


def claim(table, source_id: str, retention_days: int = DEFAULT_RETENTION_DAYS,
          **fields: Any) -> Optional[str]:
    """
    Atomically claim a source id by INSERTING its row. Returns the new row's
    etag if this call created it, None if it already existed (a concurrent claim
    won the race). Insert is atomic in Table Storage: of two concurrent inserts
    for the same (PartitionKey, RowKey), exactly one succeeds and the other gets
    409. The winner's etag lets it later release its own claim (see reclaim)
    without ever touching a row a newer invocation has re-claimed.

    This replaces the Dataverse alternate key's atomic role at the ingestion
    level, so a re-fired SharePoint trigger cannot produce two Dynamics rows for
    one upload even when the two runs are perfectly concurrent.
    """
    from azure.core.exceptions import ResourceExistsError

    pk, rk = keys_for_source_id(source_id)
    now = datetime.now(timezone.utc)
    entity: Dict[str, Any] = {
        "PartitionKey": pk,
        "RowKey": rk,
        "SharePointItemId": source_id,
        "IngestedUtc": now,
        "LastUpdatedUtc": now,
        "ExpiresAt": now + timedelta(days=retention_days),
    }
    entity.update({k: ("" if v is None else v) for k, v in fields.items()})
    try:
        metadata = table.create_entity(entity)
        return (metadata or {}).get("etag")
    except ResourceExistsError:
        return None


def is_stale(entity: Optional[Dict[str, Any]], lease_seconds: int) -> bool:
    """
    True if the row's LastUpdatedUtc is older than lease_seconds. A row left at
    Received with no decision is either a crashed prior run (safe to resume) or a
    currently in-flight run (must not be resumed). The ledger alone cannot tell
    them apart, so a staleness lease decides: older than the lease -> crashed,
    resume; within the lease -> assume in-flight, skip. CU runs complete in
    seconds, so the default lease is comfortably long.
    """
    if not entity:
        return True
    ts = entity.get("LastUpdatedUtc") or entity.get("IngestedUtc")
    if ts is None:
        return True
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return True
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() > lease_seconds


def entity_etag(entity: Any) -> Optional[str]:
    """The etag of an entity returned by get_row, tolerant of SDK differences:
    azure-data-tables surfaces it as entity.metadata["etag"]; a plain dict row
    may carry it inline as "odata.etag"."""
    meta = getattr(entity, "metadata", None)
    if isinstance(meta, dict) and meta.get("etag"):
        return meta["etag"]
    if isinstance(entity, dict):
        return entity.get("odata.etag")
    return None


def reclaim(table, source_id: str, etag: Optional[str],
            retention_days: int = DEFAULT_RETENTION_DAYS, **fields: Any) -> Optional[str]:
    """
    Etag-conditioned atomic merge on an EXISTING row. Used two ways:
      - re-claim a row for re-processing (a re-uploaded item, or a crashed run
        older than the lease), conditioned on the etag the caller read;
      - release a failed claim (Status=Failed), conditioned on the etag the
        caller's own claim/reclaim returned, so a delayed release can never
        touch a row a newer invocation has since re-claimed.
    Returns the new etag if this call won the row; None if a concurrent
    invocation updated (or deleted) it first -- the loser must skip, so a
    double-fired trigger cannot re-process the same item twice. This mirrors the
    atomic insert in claim() for first-seen items. Merge mode preserves
    IngestedUtc and a recorded DynamicsRecordId.
    """
    from azure.core import MatchConditions
    from azure.core.exceptions import (
        HttpResponseError,
        ResourceModifiedError,
        ResourceNotFoundError,
    )
    from azure.data.tables import UpdateMode

    if not etag:
        return None
    pk, rk = keys_for_source_id(source_id)
    now = datetime.now(timezone.utc)
    entity: Dict[str, Any] = {
        "PartitionKey": pk,
        "RowKey": rk,
        "SharePointItemId": source_id,
        "LastUpdatedUtc": now,
        "ExpiresAt": now + timedelta(days=retention_days),
    }
    entity.update({k: ("" if v is None else v) for k, v in fields.items()})
    try:
        metadata = table.update_entity(entity, mode=UpdateMode.MERGE, etag=etag,
                                       match_condition=MatchConditions.IfNotModified)
        return (metadata or {}).get("etag") or etag
    except (ResourceModifiedError, ResourceNotFoundError):
        return None
    except HttpResponseError as exc:  # some SDK versions raise the base type for 412
        if getattr(exc, "status_code", None) == 412:
            return None
        raise


def upsert(
    table,
    source_id: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    **fields: Any,
) -> Tuple[str, str, bool]:
    """
    Idempotent merge upsert keyed on the source id. Returns (pk, rk, inserted).

    On an existing row the original IngestedUtc is preserved and a previously
    recorded DynamicsRecordId is not blanked when this call does not supply one.
    Unknown keyword fields are written as entity properties (schemaless).
    """
    from azure.data.tables import UpdateMode

    pk, rk = keys_for_source_id(source_id)
    now = datetime.now(timezone.utc)
    existing = get_row(table, source_id)

    entity: Dict[str, Any] = {
        "PartitionKey": pk,
        "RowKey": rk,
        "SharePointItemId": source_id,
        "LastUpdatedUtc": now,
        "ExpiresAt": now + timedelta(days=retention_days),
    }
    entity.update({k: ("" if v is None else v) for k, v in fields.items()})

    if existing is not None:
        entity["IngestedUtc"] = existing.get("IngestedUtc", now)
        if not fields.get("DynamicsRecordId") and existing.get("DynamicsRecordId"):
            entity["DynamicsRecordId"] = existing["DynamicsRecordId"]
        inserted = False
    else:
        entity["IngestedUtc"] = now
        inserted = True

    table.upsert_entity(entity, mode=UpdateMode.MERGE)
    return pk, rk, inserted
