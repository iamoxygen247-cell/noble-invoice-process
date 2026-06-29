#!/usr/bin/env python3
r"""
local_test.py - POST one invoice to the decision-engine Function AND record the
result the way step24_test.py does (a raw-result JSON plus a matrix scorecard).

WHAT CHANGED vs the original local_test.py
  The original only printed the HTTP response. It now also persists, into an output
  folder (default .\out):
    - <result-json>   the Function's full decision JSON for this run (overwritten
                      each run, like step24's result.json). This is the Function's
                      DECISION, not the raw Content Understanding dump - see the note
                      at the bottom of this docstring.
    - <scorecard>     the SAME matrix scorecard step24 writes (column 0 = field
                      labels, one column per run), reconstructed from the Function
                      response. Supports --append-scorecard and --run-label exactly
                      like step24.

  The matrix machinery is imported from scorecard.py (the single source shared with
  step24_test.py), so there is no second copy of that logic. The field policy
  (which fields are critical, the print order) is imported from functionapp/gates.py
  so the scorecard's gate column matches what the Function actually decided.

  This script is still standard-library only for its OWN logic (urllib, base64,
  json). scorecard.py and gates.py are also stdlib-only, so nothing new is installed.

Stdlib only. Reads a PDF, base64-encodes it, and POSTs the binary-transport request
the Power Automate flow will send.

Examples:
  # local host (func start), random source id, writes .\out\local_result.json + .\out\local_scorecard.csv
  python local_test.py --file ".\samples\invoice1.pdf"

  # accumulate runs as columns in the matrix (compare samples / thresholds side by side)
  python local_test.py --file ".\samples\invoice1.pdf" --append-scorecard --run-label invoice1_func
  python local_test.py --file ".\samples\invoice2.pdf" --append-scorecard --run-label invoice2_func

  # fixed source id, then run again to prove gate A1 short-circuits (no scorecard column on the skip)
  python local_test.py --file ".\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77
  python local_test.py --file ".\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77 --reprocess

  # deployed function (pass the function key)
  python local_test.py --file ".\samples\invoice1.pdf" --endpoint https://<app>.azurewebsites.net/api/process-invoice --code <FUNCTION_KEY>

Assumed repo layout (so the imports resolve):
    <repo>\scripts\local_test.py      <- this file
    <repo>\scripts\scorecard.py       <- shared matrix module (sibling, REQUIRED)
    <repo>\functionapp\gates.py       <- field policy (CRITICAL_FIELDS, FIELD_PRINT_ORDER)
  Run it from the repo root so .\out lands at <repo>\out, or pass --out-dir.

Note on "the same result" as step24:
  step24 calls Content Understanding directly and dumps the RAW CU result to
  result.json. This script calls the FUNCTION, which returns its decision (fields are
  summarised to {value, confidence}; the routing decision and GST are already
  computed). For every router category the Function and step24 treat identically, the
  scorecard this script writes is byte-for-byte the same format AND the same values as
  step24's. The result JSON written here is the Function's decision, which is the more
  useful artifact for testing the deployed path (it also carries the A1 status and
  ledger keys). If you specifically want the raw CU JSON too, the Function must echo it
  - pass --include-raw and add the one-line change described in the README/your notes;
  this script will then also write <stem>_raw.json when the response contains it.

  One known, deliberate divergence: step24 has a per-category critical-field override
  (CATEGORY_CRITICAL_FIELDS) that makes payment_due_date critical for the "utilities"
  category; the deployed gates.py has NO per-category logic and uses one global
  CRITICAL_FIELDS for every category. This script reports what the Function actually
  decided (global set), so for a "utilities" invoice its critical_fields /
  payment_due_date.gate / routing rows reflect the Function and will differ from
  step24. That is a real gates.py-vs-step24 gap, not a quirk of this script - resolve
  it by either porting CATEGORY_CRITICAL_FIELDS into gates.py or dropping it from
  step24. Every other category (general_invoice, trades, landscaping, ...) matches.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# --- make scorecard.py (sibling) and gates.py (functionapp) importable -------
_HERE = pathlib.Path(__file__).resolve().parent          # ...\scripts
_REPO = _HERE.parent                                     # repo root
for _cand in (_HERE, _REPO / "functionapp"):
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

try:
    import scorecard
except ImportError as exc:  # pragma: no cover - configuration error
    raise SystemExit(
        "Could not import scorecard.py. Place it next to this file "
        f"(expected {_HERE / 'scorecard.py'}). Original error: {exc}"
    )

# Field policy: prefer the single source in functionapp/gates.py so the scorecard's
# gate column matches the Function. Fall back to a local copy (kept in sync) if the
# function project is not on the path, with a loud note.
_GATES_SOURCE: Optional[str] = None
try:
    import gates as _gates
    import field_policy as _field_policy

    # Critical-field policy moved to field_policy (bill-type buckets). Use the
    # commercial superset (base + delta) for the scorecard's static gate column.
    CRITICAL_FIELDS = list(_field_policy.critical_fields("commercial"))
    FIELD_PRINT_ORDER = list(_gates.FIELD_PRINT_ORDER)
    _GATES_SOURCE = getattr(_gates, "__file__", "gates")
except Exception:  # ImportError or attribute drift
    CRITICAL_FIELDS = [
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "gst_amount",
        "total_invoice_amount",
        "service_address",
    ]
    FIELD_PRINT_ORDER = [
        "vendor_name",
        "invoice_date",
        "payment_due_date",
        "invoice_number",
        "po_or_job_number",
        "job_number",
        "subtotal_before_tax",
        "gst_rate",
        "gst_amount",
        "total_invoice_amount",
        "service_address",
        "invoice_description",
        "is_handwritten",
        "vendor_category",
        "anomaly_flag",
    ]

DEFAULT_ENDPOINT = "http://localhost:7071/api/process-invoice"
DEFAULT_FIELD_THRESHOLD = 0.75
DEFAULT_OUT_DIR = "out"
DEFAULT_RESULT_JSON = "local_result.json"
DEFAULT_SCORECARD = "local_scorecard.csv"
RAW_CU_KEY = "cuResult"  # key the Function would use if --include-raw is wired


# -----------------------------------------------------------------------------
# Scorecard reconstruction from the Function response
# -----------------------------------------------------------------------------


def field_gate_from_summary(field_name: str, entry: Optional[Dict[str, Any]], threshold: float) -> str:
    """
    Reproduce step24's field_gate_for_scorecard, but read from the Function's
    summarised field shape {"value": ..., "confidence": ...} instead of a raw CU
    field object. Same decisions, same strings.
    """
    if field_name not in CRITICAL_FIELDS:
        if field_name == "payment_due_date":
            return "not critical - Logic App defaults to invoice_date + 30 days"
        return "not critical"

    if entry is None:
        return "REVIEW: missing field"

    value = entry.get("value")
    confidence = entry.get("confidence")

    if value is None or (isinstance(value, str) and value.strip() == ""):
        return "REVIEW: empty value"
    if confidence is None:
        return "REVIEW: missing confidence"
    if isinstance(confidence, (int, float)) and confidence < threshold:
        return f"REVIEW: confidence {confidence:.3f} < {threshold:.2f}"
    return "pass"


def build_scorecard_pairs(
    response: Dict[str, Any],
    source: str,
    input_type: str,
    threshold: float,
) -> Tuple[List[Tuple[str, str]], str]:
    """
    Build the same (label, value) rows step24's append_scorecard builds, from the
    Function's decision JSON. Returns (pairs, run_id).
    """
    run_id = scorecard.run_id_utc()
    fields: Dict[str, Any] = response.get("fields") or {}
    gst: Dict[str, Any] = response.get("gst") or {}

    # The active critical-field set is the Function's (gates.CRITICAL_FIELDS), so the
    # scorecard's critical_fields/.gate columns stay consistent with the routing
    # decision the Function actually returned. The payment_due_date_gate row is
    # computed the same WAY step24 computes it (from the active set), so the two match
    # for every category the Function and step24 treat identically. See the note in the
    # module docstring about the utilities per-category override that step24 has and the
    # deployed gates.py does not.
    payment_due_date_gate = (
        "critical for this category"
        if "payment_due_date" in CRITICAL_FIELDS
        else "not critical - Logic App defaults to invoice_date + 30 days"
    )

    pairs: List[Tuple[str, str]] = [
        ("run_id_utc", run_id),
        ("source", source),
        ("input_type", input_type),
        ("router_category", str(response.get("routerCategory", ""))),
        ("router_category_path", str(response.get("routerCategoryPath", ""))),
        ("router_confidence_gate", "skipped - not consistently available in CU router result"),
        ("effective_document_type", str(response.get("effectiveDocumentType", ""))),
        ("analyzer_used", str(response.get("analyzerUsed", ""))),
        ("critical_fields", ", ".join(CRITICAL_FIELDS)),
        ("payment_due_date_gate", payment_due_date_gate),
    ]

    ordered_field_names = FIELD_PRINT_ORDER + [n for n in fields.keys() if n not in FIELD_PRINT_ORDER]
    seen: set = set()
    for field_name in ordered_field_names:
        if field_name in seen:
            continue
        seen.add(field_name)

        entry = fields.get(field_name)
        entry = entry if isinstance(entry, dict) else None
        value = entry.get("value") if entry else None
        confidence = entry.get("confidence") if entry else None

        pairs.append((field_name, scorecard.csv_scalar(value)))
        pairs.append(
            (f"{field_name}.confidence", "MISSING" if confidence is None else f"{float(confidence):.3f}")
        )
        pairs.append(
            (f"{field_name}.gate", field_gate_from_summary(field_name, entry, threshold))
        )

        if field_name == "invoice_description":
            pairs.append(("invoice_description.word_count", scorecard.csv_scalar(scorecard.count_words(value))))
            pairs.append(("invoice_description.length_gate", scorecard.invoice_description_gate(value)))

    pairs.extend(
        [
            ("amount_excluding_gst_calculated", scorecard.csv_scalar(gst.get("amount_excluding_gst_calculated"))),
            ("expected_gst_5_percent", scorecard.csv_scalar(gst.get("expected_gst_5_percent"))),
            ("actual_gst", scorecard.csv_scalar(gst.get("actual_gst"))),
            ("gst_difference", scorecard.csv_scalar(gst.get("difference"))),
            ("gst_math_valid", scorecard.csv_scalar(gst.get("gst_math_valid"))),
            ("routing_decision", str(response.get("routingDecision", ""))),
            ("review_reasons", " | ".join(response.get("reviewReasons") or [])),
        ]
    )
    return pairs, run_id


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------


def post_invoice(url: str, body: Dict[str, Any]) -> Tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8")


# -----------------------------------------------------------------------------
# CLI / main
# -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="POST one invoice to the decision-engine Function and record the result.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="Local PDF/image to send as binary (contentBase64).")
    src.add_argument("--url", help="Blob SAS URL (ad-hoc test path instead of binary).")

    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"Function URL. Default: {DEFAULT_ENDPOINT}")
    ap.add_argument("--code", default="", help="Function key (?code=...) for a deployed app.")
    ap.add_argument("--source-id", default=None, help="SharePoint item id to key on. Default: a random GUID.")
    ap.add_argument("--reprocess", action="store_true", help="Bypass gate A1.")
    ap.add_argument(
        "--field-threshold",
        type=float,
        default=DEFAULT_FIELD_THRESHOLD,
        help=(
            f"B4 critical-field threshold. Sent to the Function as fieldThreshold AND used to "
            f"recompute the scorecard's gate column, so both agree. Default: {DEFAULT_FIELD_THRESHOLD}"
        ),
    )

    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"Output folder. Default: {DEFAULT_OUT_DIR} (relative to where you run this)")
    ap.add_argument("--result-json", default=DEFAULT_RESULT_JSON, help=f"Result JSON filename. Default: {DEFAULT_RESULT_JSON}")
    ap.add_argument("--scorecard", default=DEFAULT_SCORECARD, help=f"Scorecard CSV filename. Default: {DEFAULT_SCORECARD}")
    ap.add_argument(
        "--append-scorecard",
        action="store_true",
        help="Add this run as a new rightmost column in the scorecard matrix (default: overwrite to a fresh matrix).",
    )
    ap.add_argument(
        "--run-label",
        default=None,
        help="Optional column header for this run (e.g. invoice1_func). Defaults to the UTC run id.",
    )
    ap.add_argument(
        "--include-raw",
        action="store_true",
        help=f"Send includeRaw:true and, if the response carries '{RAW_CU_KEY}', also write <stem>_raw.json (needs the optional Function change).",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    if _GATES_SOURCE is None:
        print(
            "[note] could not import functionapp/gates.py; using this script's built-in field policy. "
            "If you have changed CRITICAL_FIELDS/FIELD_PRINT_ORDER in gates.py, run from the repo root so "
            "the scorecard's gate column stays in sync."
        )

    source_id = args.source_id or str(uuid.uuid4())
    body: Dict[str, Any] = {
        "sourceId": source_id,
        "reprocess": args.reprocess,
        "fieldThreshold": args.field_threshold,
    }
    if args.include_raw:
        body["includeRaw"] = True

    input_type = "file" if args.file else "url"
    if args.file:
        path = pathlib.Path(args.file)
        if not path.is_file():
            raise SystemExit(f"File not found: {path}")
        body["fileName"] = path.name
        body["contentBase64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        source = str(path)
    else:
        body["url"] = args.url
        source = args.url

    url = args.endpoint + (f"?code={args.code}" if args.code else "")

    print(f"source id: {source_id}")
    print(f"field threshold: {args.field_threshold:.2f}")
    status, payload = post_invoice(url, body)
    print(f"HTTP {status}")

    out_dir = pathlib.Path(args.out_dir)
    result_path = out_dir / args.result_json
    scorecard_path = out_dir / args.scorecard

    # Parse the response. If it is not JSON (e.g. an HTML 500), persist the raw text
    # for debugging and stop - there is nothing to score.
    try:
        response: Dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError:
        out_dir.mkdir(parents=True, exist_ok=True)
        result_path.write_text(payload, encoding="utf-8")
        print(payload)
        print(f"\nNon-JSON response saved to {result_path}; no scorecard written.")
        return 1

    print(json.dumps(response, indent=2, ensure_ascii=False))

    # 1) Always persist the Function's decision JSON (the local analogue of result.json).
    scorecard.write_json_atomic(result_path, response)
    print(f"\nResult JSON saved to {result_path} (overwrite mode)")

    # Optional raw CU dump, only if the Function was wired to echo it.
    if args.include_raw:
        raw = response.get(RAW_CU_KEY)
        if raw is not None:
            raw_path = result_path.with_name(result_path.stem + "_raw" + result_path.suffix)
            scorecard.write_json_atomic(raw_path, raw)
            print(f"Raw CU result saved to {raw_path}")
        else:
            print(f"[note] --include-raw set but the response has no '{RAW_CU_KEY}'; the Function did not echo the raw CU result.")

    # 2) Scorecard - only when Content Understanding actually ran and returned fields.
    skipped = bool(response.get("alreadyProcessed")) or bool(response.get("skippedCU"))
    if skipped:
        print(
            "\nGate A1 short-circuited this run (alreadyProcessed): CU did not run, so there is no "
            "extraction to score - no scorecard column written. Re-run with --reprocess or a fresh "
            "--source-id to score this invoice."
        )
        return 0

    if "fields" not in response:
        # An error decision (e.g. 502 CU failure, 400 bad request): no extraction.
        print(f"\nResponse has no 'fields' (decision: {response.get('routingDecision') or response.get('error')}); no scorecard column written.")
        return 0 if status == 200 else 1

    pairs, run_id = build_scorecard_pairs(response, source, input_type, args.field_threshold)
    column_header = scorecard.derive_column_header(args.run_label, run_id)
    scorecard.write_scorecard_matrix(scorecard_path, pairs, column_header, append=args.append_scorecard)
    print(
        f"Scorecard written to {scorecard_path}"
        + (" (new column)" if args.append_scorecard else " (fresh matrix)")
        + f" - column '{column_header}'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
