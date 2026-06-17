#!/usr/bin/env python3
r"""
Phase 3 - ProcessingLog ledger test harness for the Noble invoice prototype.

Validates the Azure Table Storage ledger the Phase 4 Logic App will write to.

IDENTITY MODEL (production-aligned):
  The row identity is the stable SharePoint item identifier, passed as --source-id.
  This is Layer 1 of duplicate prevention - IDEMPOTENCY, not deduplication. It answers
  "have I already processed this exact SharePoint ingestion?" and guards against the
  polling trigger re-firing, overlapping polls, and retries. It is content-blind: the
  same invoice re-uploaded as a NEW SharePoint file gets a new item id and is a new row.

  Catching that case - the same invoice arriving by any route, including manual entry
  straight into Dynamics - is Layer 2's job: the Dataverse B5 query plus the alternate
  key on invoice number, enforced at write time in Phase 6. This ledger does not attempt
  it, and deliberately computes no content hash.

  For --source-id use the SharePoint item's UniqueId (a GUID), not the per-list integer
  Id: the GUID is globally unique, stable, and shards cleanly across partitions.

Behaviours exercised:
  - upsert a ledger row keyed on the source id (idempotent: re-running the same
    --source-id updates in place, never creates a second row)
  - point-lookup by source id (the cheap "seen this ingestion already?" check)
  - query rows whose DynamicsRecordId is empty (in-flight / stuck visibility)

Authentication (no hardcoded secrets):
  Default: DefaultAzureCredential. Locally this uses your `az login` identity; in Azure
  it uses the Logic App / Function managed identity. The identity needs the
  "Storage Table Data Contributor" role on the storage account (or the table).
  Fallback (quick prototype only): pass --use-connection-string with the account string
  in AZURE_TABLES_CONNECTION_STRING. Never paste it into this file.

Prerequisites:
  python -m pip install azure-data-tables azure-identity

Examples (use a real SharePoint item UniqueId for --source-id; the GUID below is a stand-in):
  # offline sanity check (no network, no auth)
  python phase3_ledger_test.py --self-test --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77 --file-name invoice1.pdf

  # register an ingestion
  python phase3_ledger_test.py --account stinvoicedevwestus --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77 --file-name invoice1.pdf --status Received --routing-decision HAPPY_PATH_CANDIDATE

  # re-run the same source id: proves idempotency (no duplicate row)
  python phase3_ledger_test.py --account stinvoicedevwestus --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77 --status Extracted

  # record the Dynamics row id once written (clears the stuck signal)
  python phase3_ledger_test.py --account stinvoicedevwestus --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77 --status Written --dynamics-id 4a7c0f12-aaaa-bbbb-cccc-0000000000e9

  # seen-before check, no write
  python phase3_ledger_test.py --account stinvoicedevwestus --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77 --check

  # in-flight / stuck rows (no Dynamics id yet)
  python phase3_ledger_test.py --account stinvoicedevwestus --show-stuck
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

TABLE_NAME = "ProcessingLog"
DEFAULT_RETENTION_DAYS = 90
TABLES_ENDPOINT_TEMPLATE = "https://{account}.table.core.windows.net"
CONNECTION_STRING_ENV = "AZURE_TABLES_CONNECTION_STRING"
PARTITION_PREFIX_LEN = 2

# Table Storage forbids these in PartitionKey/RowKey: / \ # ? and control characters.
_INVALID_KEY_CHARS = re.compile(r"[\u0000-\u001F\u007F-\u009F/\\#?]")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 3 ProcessingLog ledger test harness."
    )
    p.add_argument(
        "--account",
        help="Storage account name, used to build the table endpoint "
        "https://<account>.table.core.windows.net. Not required with "
        "--use-connection-string.",
    )
    p.add_argument("--table", default=TABLE_NAME, help=f"Table name. Default: {TABLE_NAME}")
    p.add_argument(
        "--source-id",
        help="Stable SharePoint item identifier (use the item UniqueId GUID). "
        "This is the row identity - the Layer 1 idempotency key.",
    )
    p.add_argument(
        "--file-name",
        default="",
        help="Original file name, stored for provenance only (not part of the key).",
    )
    p.add_argument(
        "--sharepoint-url",
        default="",
        help="SharePoint file URL or path, stored for provenance.",
    )
    p.add_argument(
        "--status",
        default="Received",
        help="Status to write: Received, Extracted, InReview, Written, Rejected.",
    )
    p.add_argument(
        "--routing-decision",
        default="",
        help="Routing decision string from the Phase 2 harness, e.g. HAPPY_PATH_CANDIDATE.",
    )
    p.add_argument(
        "--dynamics-id",
        default="",
        help="Dataverse row id, recorded once written. Empty means in-flight.",
    )
    p.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Days until ExpiresAt. Default: {DEFAULT_RETENTION_DAYS}",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Only check whether this source id already exists; do not write.",
    )
    p.add_argument(
        "--show-stuck",
        action="store_true",
        help="List rows with an empty DynamicsRecordId (in-flight / stuck).",
    )
    p.add_argument(
        "--use-connection-string",
        action="store_true",
        help=f"Authenticate with the connection string in ${CONNECTION_STRING_ENV} "
        "instead of DefaultAzureCredential.",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Offline check: print keys and the row that would be written. "
        "No network, no auth, no Azure packages required.",
    )
    return p.parse_args()


# -----------------------------------------------------------------------------
# Key derivation (no content hash)
# -----------------------------------------------------------------------------


def sanitize_key(value: str) -> str:
    """Make a string safe for use as a Table Storage key segment."""
    cleaned = value.strip().strip("{}")
    return _INVALID_KEY_CHARS.sub("-", cleaned)


def keys_for_source_id(source_id: str) -> tuple[str, str]:
    # RowKey = the sanitized SharePoint item id. PartitionKey = a short prefix of it,
    # to spread write load while keeping the lookup a single point query derived from
    # the source id alone. A GUID UniqueId shards well on a 2-char prefix; at this
    # volume a fixed partition would also be fine. A natural production alternative is
    # PartitionKey = SharePoint list/library id, RowKey = item id.
    rk = sanitize_key(source_id)
    pk = rk[:PARTITION_PREFIX_LEN] if rk else "00"
    return pk, rk


def build_entity(args: argparse.Namespace, source_id: str) -> dict:
    pk, rk = keys_for_source_id(source_id)
    now = datetime.now(timezone.utc)
    return {
        "PartitionKey": pk,
        "RowKey": rk,
        "SharePointItemId": source_id,
        "FileName": args.file_name or "",
        "SharePointUrl": args.sharepoint_url or "",
        "Status": args.status,
        "RoutingDecision": args.routing_decision,
        "DynamicsRecordId": args.dynamics_id,
        "IngestedUtc": now,
        "LastUpdatedUtc": now,
        "ExpiresAt": now + timedelta(days=args.retention_days),
    }


# -----------------------------------------------------------------------------
# Azure Table client
# -----------------------------------------------------------------------------


def get_table_client(args: argparse.Namespace):
    from azure.data.tables import TableServiceClient

    if args.use_connection_string:
        conn = os.getenv(CONNECTION_STRING_ENV)
        if not conn:
            raise SystemExit(
                f"--use-connection-string was set but ${CONNECTION_STRING_ENV} is empty."
            )
        service = TableServiceClient.from_connection_string(conn)
    else:
        if not args.account:
            raise SystemExit(
                "Provide --account <storage-account-name>, or use --use-connection-string."
            )
        from azure.identity import DefaultAzureCredential

        endpoint = TABLES_ENDPOINT_TEMPLATE.format(account=args.account)
        service = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())

    service.create_table_if_not_exists(args.table)
    return service.get_table_client(args.table)


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def check_only(table, source_id: str) -> int:
    from azure.core.exceptions import ResourceNotFoundError

    pk, rk = keys_for_source_id(source_id)
    try:
        existing = table.get_entity(partition_key=pk, row_key=rk)
    except ResourceNotFoundError:
        print(f"NOT SEEN: source id {source_id} is not in {table.table_name}.")
        return 0
    print(f"ALREADY SEEN: source id {source_id}")
    dynamics = existing.get("DynamicsRecordId") or "(empty - in flight)"
    print(f"  Status={existing.get('Status')}  DynamicsRecordId={dynamics}")
    print(f"  IngestedUtc={existing.get('IngestedUtc')}  LastUpdatedUtc={existing.get('LastUpdatedUtc')}")
    return 0


def upsert(table, args: argparse.Namespace, source_id: str) -> int:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.data.tables import UpdateMode

    pk, rk = keys_for_source_id(source_id)
    now = datetime.now(timezone.utc)

    try:
        existing = table.get_entity(partition_key=pk, row_key=rk)
    except ResourceNotFoundError:
        existing = None

    entity = build_entity(args, source_id)
    if existing is not None:
        # this is an update, not a first sighting: keep the original ingestion time
        entity["IngestedUtc"] = existing.get("IngestedUtc", now)
        # do not blank a previously recorded Dynamics id if this call did not supply one
        if not args.dynamics_id and existing.get("DynamicsRecordId"):
            entity["DynamicsRecordId"] = existing["DynamicsRecordId"]
        verb = "UPDATED (idempotent - no duplicate row created)"
    else:
        verb = "INSERTED (first sighting)"

    entity["LastUpdatedUtc"] = now
    table.upsert_entity(entity, mode=UpdateMode.MERGE)

    print(f"{verb}: PartitionKey={pk} RowKey={rk}")
    print(f"  Status={entity['Status']}  RoutingDecision={entity['RoutingDecision'] or '-'}")
    print(f"  DynamicsRecordId={entity['DynamicsRecordId'] or '(empty - in flight)'}")
    print(f"  IngestedUtc={entity['IngestedUtc']}  ExpiresAt={entity['ExpiresAt']}")
    return 0


def show_stuck(table) -> int:
    # An empty DynamicsRecordId means the write-to-Dynamics step never completed.
    rows = list(table.query_entities("DynamicsRecordId eq ''"))
    if not rows:
        print("No stuck rows: every ledger entry has a DynamicsRecordId.")
        return 0
    print(f"{len(rows)} in-flight / stuck row(s) (no DynamicsRecordId):")
    for r in rows:
        print(
            f"  {r.get('RowKey')}  Status={r.get('Status')}  "
            f"File={r.get('FileName')}  Ingested={r.get('IngestedUtc')}"
        )
    return 0


def self_test(args: argparse.Namespace) -> int:
    if not args.source_id:
        raise SystemExit("--self-test needs --source-id (the SharePoint item id to key on).")
    pk, rk = keys_for_source_id(args.source_id)
    print(f"source id:    {args.source_id}")
    print(f"PartitionKey: {pk}")
    print(f"RowKey:       {rk}")
    print("Row that WOULD be written:")
    entity = build_entity(args, args.source_id)
    serialisable = {
        k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in entity.items()
    }
    print(json.dumps(serialisable, indent=2))
    print("Stuck-row query filter: DynamicsRecordId eq ''")
    return 0


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> int:
    args = parse_args()

    if args.self_test:
        return self_test(args)

    if args.show_stuck:
        table = get_table_client(args)
        return show_stuck(table)

    if not args.source_id:
        raise SystemExit("Provide --source-id (to register or check an ingestion) or --show-stuck.")

    print(f"Source id: {args.source_id}")
    table = get_table_client(args)

    if args.check:
        return check_only(table, args.source_id)
    return upsert(table, args, args.source_id)


if __name__ == "__main__":
    raise SystemExit(main())
