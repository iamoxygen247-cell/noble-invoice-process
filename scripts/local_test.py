#!/usr/bin/env python3
r"""
local_test.py — POST one invoice to the decision-engine Function.

Stdlib only (no extra packages). Reads a PDF, base64-encodes it, and POSTs the
binary-transport request the Logic App will send.

Examples:
  # local host (func start), random source id (new ingestion each run)
  python local_test.py --file ".\samples\invoice1.pdf"

  # fixed source id, then run again to prove gate A1 short-circuits
  python local_test.py --file ".\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77
  python local_test.py --file ".\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77

  # force a re-run past A1
  python local_test.py --file ".\samples\invoice1.pdf" --source-id <same> --reprocess

  # deployed function (pass the function key)
  python local_test.py --file ".\samples\invoice1.pdf" --endpoint https://<app>.azurewebsites.net/api/process-invoice --code <FUNCTION_KEY>
"""
import argparse
import base64
import json
import pathlib
import urllib.request
import uuid


def main() -> int:
    ap = argparse.ArgumentParser(description="POST one invoice to the decision-engine Function.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="Local PDF/image to send as binary (contentBase64).")
    src.add_argument("--url", help="Blob SAS URL (ad-hoc test path instead of binary).")
    ap.add_argument("--endpoint", default="http://localhost:7071/api/process-invoice",
                    help="Function URL. Default: local host.")
    ap.add_argument("--code", default="", help="Function key (?code=...) for a deployed app.")
    ap.add_argument("--source-id", default=None,
                    help="SharePoint item id to key on. Default: a random GUID.")
    ap.add_argument("--reprocess", action="store_true", help="Bypass gate A1.")
    args = ap.parse_args()

    source_id = args.source_id or str(uuid.uuid4())
    body = {"sourceId": source_id, "reprocess": args.reprocess}

    if args.file:
        path = pathlib.Path(args.file)
        if not path.is_file():
            raise SystemExit(f"File not found: {path}")
        body["fileName"] = path.name
        body["contentBase64"] = base64.b64encode(path.read_bytes()).decode("ascii")
    else:
        body["url"] = args.url

    url = args.endpoint + (f"?code={args.code}" if args.code else "")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print(f"source id: {source_id}")
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        status = err.code
        payload = err.read().decode("utf-8")

    print(f"HTTP {status}")
    try:
        print(json.dumps(json.loads(payload), indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
