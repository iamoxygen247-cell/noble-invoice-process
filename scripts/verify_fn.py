#!/usr/bin/env python3
"""
verify_fn.py - exercise the deployed Noble invoice Function in Azure.

stdlib only; Windows / PowerShell friendly. A fresh GUID sourceId is generated
per run, so gate A1 never short-circuits unless you pass --source-id yourself.
The --insecure flag uses an unverified TLS context, which is the Python
equivalent of curl's --ssl-no-revoke --insecure for a TLS-intercepting
corporate proxy.

Examples (PowerShell):
  python verify_fn.py --base-url "https://<host>" --key "<func-key>" --bad-payload --insecure
  python verify_fn.py --base-url "https://<host>" --key "<func-key>" --file "..\\samples\\invoice1.pdf" --insecure
  python verify_fn.py --base-url "https://<host>" --key "<func-key>" --file "..\\samples\\invoice1.pdf" --source-id <guid> --reprocess --insecure
"""
import argparse, base64, json, ssl, sys, urllib.request, urllib.error, uuid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True,
                    help="e.g. https://func-...azurewebsites.net")
    ap.add_argument("--key", help="function key (sent as the x-functions-key header)")
    ap.add_argument("--file", help="path to an invoice PDF")
    ap.add_argument("--source-id", help="override sourceId (default: a fresh GUID)")
    ap.add_argument("--reprocess", action="store_true", help="bypass gate A1")
    ap.add_argument("--bad-payload", action="store_true",
                    help="POST {} to force the HTTP 400 validation path")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (corporate proxy)")
    args = ap.parse_args()

    url = args.base_url.rstrip("/") + "/api/process-invoice"

    sid = None
    if args.bad_payload:
        body = {}
    else:
        if not args.file:
            ap.error("--file is required unless --bad-payload")
        with open(args.file, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        sid = args.source_id or str(uuid.uuid4())
        body = {"sourceId": sid,
                "contentBase64": b64,
                "fileName": args.file.replace("\\", "/").split("/")[-1]}
        if args.reprocess:
            body["reprocess"] = True

    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    if args.key:
        req.add_header("x-functions-key", args.key)

    ctx = ssl._create_unverified_context() if args.insecure else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            status, payload = resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read().decode("utf-8")
    except Exception as e:
        print("REQUEST FAILED:", e)
        sys.exit(2)

    print("HTTP", status)
    try:
        obj = json.loads(payload)
        print(json.dumps(obj, indent=2)[:4000])
        if "routingDecision" in obj:
            print("\n--> routingDecision:", obj.get("routingDecision"),
                  "| status:", obj.get("status"),
                  "| alreadyProcessed:", obj.get("alreadyProcessed"))
            if obj.get("ledgerWriteError"):
                print("--> WARNING ledgerWriteError:", obj.get("ledgerWriteError"))
        if sid:
            print("--> sourceId:", sid)
    except ValueError:
        print(payload[:4000])


if __name__ == "__main__":
    main()
