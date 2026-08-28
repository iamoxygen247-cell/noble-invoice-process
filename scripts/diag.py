#!/usr/bin/env python3
r"""
diag.py - inspect a processed invoice's diagnostics, or replay a stored raw
Content Understanding result through the gates offline.

Two modes:

  --source-id <guid>   Print the InvoiceExtractProcessLog row for the SharePoint
                       item, list its diagnostics blobs ({rk}/<ts>-raw.json /
                       -decision.json), and download them to --out-dir.
                       Uses the same storage config as the Function:
                       AZURE_TABLES_CONNECTION_STRING (Azurite locally), or
                       AZURE_STORAGE_ACCOUNT + DefaultAzureCredential (az login),
                       plus AZURE_TABLES_TABLE / DIAGNOSTICS_CONTAINER overrides.

  --replay <raw.json>  Load a stored raw CU result and run gates.evaluate on it
                       OFFLINE (no CU call, no storage). Prints the decision
                       JSON. This reproduces the Function's decision for that
                       run, and is the way to regression-test gates/field_policy
                       changes against real historical responses.
                       On a run that was B2-rescued there are two raw blobs:
                       -raw.json is the router response and replays to the
                       pre-rescue REJECT, while -raw-rescue.json replays to the
                       extraction behind the decision that was actually returned.
                       Such rows are stamped RouterCategory='other' in the ledger.

Not named inspect.py: that would shadow the stdlib module the Azure SDKs import.

Examples (PowerShell, from the repo root):
  .\.venv\Scripts\python.exe scripts\diag.py --source-id f96b1e69-3466-451f-8833-ba63cee9b835
  .\.venv\Scripts\python.exe scripts\diag.py --replay out\diag\f96b1e69-...\20260710T031500Z-raw.json
  .\.venv\Scripts\python.exe scripts\diag.py --replay ...-raw.json --field-threshold 0.8 --file-name invoice1.pdf

Assumed repo layout (so the imports resolve):
    <repo>\scripts\diag.py            <- this file
    <repo>\functionapp\ledger.py      <- table client + row keys
    <repo>\functionapp\diagnostics.py <- blob container client
    <repo>\functionapp\gates.py       <- gates.evaluate (replay)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# The decision JSON now carries Traditional Chinese narrative fields and is dumped
# to stdout; a redirected stdout on Windows defaults to cp1252 and would raise
# UnicodeEncodeError. Console stdout is already UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
    pass

# --- make the functionapp modules importable ---------------------------------
_HERE = pathlib.Path(__file__).resolve().parent          # ...\scripts
_REPO = _HERE.parent                                     # repo root
_FUNCTIONAPP = _REPO / "functionapp"
if str(_FUNCTIONAPP) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONAPP))

import cu_client  # noqa: E402
import diagnostics  # noqa: E402
import field_policy  # noqa: E402
import gates  # noqa: E402
import ledger  # noqa: E402


def inspect_source(source_id: str, out_dir: pathlib.Path) -> int:
    pk, rk = ledger.keys_for_source_id(source_id)
    print(f"sourceId:     {source_id}")
    print(f"PartitionKey: {pk}")
    print(f"RowKey:       {rk}")

    table = ledger.get_table_client()
    row = ledger.get_row(table, source_id)
    if row is None:
        print("\nLedger row: NOT FOUND (item never claimed the A1 gate)")
    else:
        print("\nLedger row:")
        for key in sorted(row):
            if key in ("PartitionKey", "RowKey", "odata.etag"):
                continue
            print(f"  {key}: {row[key]}")

    container = diagnostics.get_container_client()
    blobs = sorted(b.name for b in container.list_blobs(name_starts_with=f"{rk}/"))
    if not blobs:
        print(f"\nDiagnostics blobs: none under {rk}/ in container "
              f"'{diagnostics.container_name()}' (run predates the sidecar, "
              "or the best-effort write failed - check the Function logs)")
        return 1 if row is None else 0

    target = out_dir / rk
    target.mkdir(parents=True, exist_ok=True)
    print(f"\nDiagnostics blobs ({len(blobs)}), downloading to {target}:")
    for name in blobs:
        data = container.download_blob(name).readall()
        local = target / name.split("/", 1)[1]
        local.write_bytes(data)
        print(f"  {name}  ->  {local}  ({len(data):,} bytes)")
    return 0


def replay(raw_path: pathlib.Path, field_threshold: float, file_name: str) -> int:
    full = json.loads(raw_path.read_text(encoding="utf-8"))
    result = gates.evaluate(
        full,
        field_threshold,
        cu_client.general_invoice_analyzer_id(),
        file_name=file_name,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a processed invoice's diagnostics, or replay a stored raw CU result offline."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source-id", help="SharePoint item UniqueId GUID to look up")
    mode.add_argument("--replay", type=pathlib.Path, metavar="RAW_JSON",
                      help="path to a stored <ts>-raw.json to run through gates.evaluate offline")
    parser.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path("out") / "diag",
                        help=r"download folder for --source-id (default .\out\diag)")
    parser.add_argument("--field-threshold", type=float, default=field_policy.THRESHOLD,
                        help=f"B4 critical-field threshold for --replay (default {field_policy.THRESHOLD})")
    parser.add_argument("--file-name", default="",
                        help="original SharePoint filename for --replay (feeds the municipal "
                             "invoice-number fallback)")
    args = parser.parse_args(argv)

    if args.replay is not None:
        if not args.replay.is_file():
            parser.error(f"--replay file not found: {args.replay}")
        return replay(args.replay, args.field_threshold, args.file_name)
    return inspect_source(args.source_id, args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
