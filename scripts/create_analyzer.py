"""
create_analyzer.py — (re)provision a Content Understanding analyzer from a JSON
definition in analyzers/.

Editing an analyzers/*.json file changes nothing in the CU service on its own;
run this to push the definition so the change takes effect. By default it
provisions the general invoice analyzer (the one that owns po_or_job_number).

Endpoint/auth mirror functionapp/cu_client.py: AZURE_CU_KEY if set, otherwise
DefaultAzureCredential (your `az login` locally, managed identity in Azure);
endpoint/api-version come from the same env vars or the same defaults. The
identity needs 'Cognitive Services User' on the CU resource. allow_replace=True,
so an existing analyzer with the same id is overwritten.

Run from the repo root, after `az login`:
    .\\.venv\\Scripts\\python.exe scripts\\create_analyzer.py
    .\\.venv\\Scripts\\python.exe scripts\\create_analyzer.py --file analyzers\\create-router-analyzer.json --analyzer-id invoicerouter
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

# Reuse the canonical CU endpoint/api-version from functionapp/cu_client.py so
# this script targets exactly what the Function uses.
_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
_FUNCTIONAPP = _REPO / "functionapp"
if _FUNCTIONAPP.is_dir() and str(_FUNCTIONAPP) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONAPP))

import cu_client  # noqa: E402  -- imported after the sys.path bootstrap above


def _build_credential():
    """Key auth if AZURE_CU_KEY is set; otherwise DefaultAzureCredential -- the
    same choice cu_client makes (az login locally, managed identity in Azure)."""
    key = os.getenv("AZURE_CU_KEY")
    if key:
        from azure.core.credentials import AzureKeyCredential

        return AzureKeyCredential(key)
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def main() -> int:
    ap = argparse.ArgumentParser(description="(Re)provision a CU analyzer from a JSON definition.")
    ap.add_argument(
        "--file",
        default=str(_REPO / "analyzers" / "create-generalinvoice-analyzer.json"),
        help="Analyzer definition JSON. Default: analyzers/create-generalinvoice-analyzer.json",
    )
    ap.add_argument(
        "--analyzer-id",
        default=cu_client.general_invoice_analyzer_id(),
        help="Analyzer id to create/replace in CU. Default: %(default)s",
    )
    ap.add_argument("--endpoint", default=cu_client.endpoint(), help="CU endpoint. Default: %(default)s")
    ap.add_argument("--api-version", default=cu_client.api_version(), help="CU API version. Default: %(default)s")
    args = ap.parse_args()

    path = pathlib.Path(args.file)
    if not path.is_file():
        raise SystemExit(f"Definition file not found: {path}")
    definition = json.loads(path.read_text(encoding="utf-8"))

    from azure.ai.contentunderstanding import ContentUnderstandingClient
    from azure.core.exceptions import AzureError

    client = ContentUnderstandingClient(
        endpoint=args.endpoint,
        credential=_build_credential(),
        api_version=args.api_version,
    )

    print(f"Provisioning analyzer '{args.analyzer_id}' at {args.endpoint}")
    print(f"  from {path}")
    print("  allow_replace=True -- overwrites any existing analyzer with this id")
    try:
        poller = client.begin_create_analyzer(args.analyzer_id, resource=definition, allow_replace=True)
        result = poller.result()
    except AzureError as exc:
        raise SystemExit(f"CU provisioning failed: {exc}")
    finally:
        client.close()

    status = getattr(result, "status", None)
    print(f"Done. analyzer_id={getattr(result, 'analyzer_id', args.analyzer_id)}"
          + (f", status={status}" if status else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
