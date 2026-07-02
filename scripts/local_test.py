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
    - <scorecard>     the same matrix scorecard step24 writes (column 0 = field
                      labels, one column per run), reconstructed from the Function
                      response and rendered as an HTML table. Supports
                      --append-scorecard and --run-label exactly like step24.

  The matrix machinery is imported from scorecard.py (the single source shared with
  step24_test.py), so there is no second copy of that logic. The field policy
  (which fields are critical, the print order) is imported from functionapp/gates.py
  so the scorecard's gate column matches what the Function actually decided.

  This script is still standard-library only for its OWN logic (urllib, base64,
  json). scorecard.py and gates.py are also stdlib-only, so nothing new is installed.

Stdlib only. Reads a PDF, base64-encodes it, and POSTs the binary-transport request
the Power Automate flow will send.

Examples:
  # local host (func start), random source id, writes .\out\local_result.json + .\out\local_scorecard.html
  python local_test.py --file ".\samples\invoice1.pdf"

  # score every top-level *.pdf in a folder as one column each (fresh table; no recursion)
  python local_test.py --folder ".\samples"

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
  scorecard this script writes carries the same values as step24's (step24 writes CSV;
  this script writes the same matrix as HTML). The result JSON written here is the
  Function's decision, which is the more
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

    # Critical-field policy moved to field_policy (bill-type buckets). CRITICAL_FIELDS
    # is the commercial superset (base + delta), kept as the fallback only; each run's
    # gate column uses the bucket-aware set from active_critical_fields(response).
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
DEFAULT_SCORECARD = "local_scorecard.html"
RAW_CU_KEY = "cuResult"  # key the Function would use if --include-raw is wired


# -----------------------------------------------------------------------------
# Scorecard reconstruction from the Function response
# -----------------------------------------------------------------------------


def active_critical_fields(response: Dict[str, Any]) -> List[str]:
    """
    The critical-field set for THIS response's policy bucket, matching the
    Function's B4 gate (municipal = base only, commercial = base + delta). The
    bucket is read from the Function's own policyBucket (billType as a fallback
    for older decision JSON); a missing or unknown label fail-safes to the
    stricter commercial superset, exactly like field_policy.resolve_bucket.
    When field_policy could not be imported, the static commercial superset is
    all we have (the [note] at startup already said so).
    """
    if _GATES_SOURCE is None:
        return list(CRITICAL_FIELDS)
    label = response.get("policyBucket") or response.get("billType")
    bucket = _field_policy.resolve_bucket(str(label) if label is not None else None)
    return list(_field_policy.critical_fields(bucket))


def field_gate_from_summary(
    field_name: str,
    entry: Optional[Dict[str, Any]],
    threshold: float,
    critical: List[str],
) -> str:
    """
    Reproduce step24's field_gate_for_scorecard, but read from the Function's
    summarised field shape {"value": ..., "confidence": ...} instead of a raw CU
    field object. Same decisions, same strings. ``critical`` is the response's
    bucket-aware critical set (active_critical_fields), so a commercial-delta
    field on a municipal bill reads not-critical instead of REVIEW.
    """
    # The raw extract/generate twins are not critical on their own -- the computed final
    # (vendor_name / service_address) is. Show an informational threshold check for the
    # twins so you can see whether each source clears the bar (esp. whether extract alone
    # hits >=0.80, and whether the generate twin agrees), without implying review.
    if field_name in (
        "vendor_name_extract", "vendor_name_generate",
        "service_address_extract", "service_address_generate",
        "total_invoice_amount_extract", "total_invoice_amount_generate",
        "gst_amount_extract", "gst_amount_generate",
        "po_or_job_number_extract", "po_or_job_number_generate",
    ):
        if entry is None:
            return "not returned by CU"
        value = entry.get("value")
        confidence = entry.get("confidence")
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return "empty"
        if confidence is None:
            return "no confidence"
        if isinstance(confidence, (int, float)) and confidence < threshold:
            return f"warning: {confidence:.3f} < {threshold:.2f}"
        return "pass"

    if field_name not in critical:
        if field_name == "payment_due_date":
            return "not critical - Logic App defaults to invoice_date + 30 days"
        if field_name in CRITICAL_FIELDS:
            # In the commercial superset but not this bill's bucket: the Function's
            # B4 gate ignored it, so the scorecard must not imply review.
            return "not critical for this bill type"
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
    Build the (label, value) scorecard rows from the Function's decision JSON. Same
    row content as step24's append_scorecard (plus a bill_type row), but emitted in a
    custom review order: identity + bill type + decision summary, then the key fields,
    then the rest of the fields and router metadata. Returns (pairs, run_id).
    """
    run_id = scorecard.run_id_utc()
    fields: Dict[str, Any] = response.get("fields") or {}

    # The active critical-field set is the one the Function's B4 gate used for THIS
    # response (bucket-aware: municipal = base only, commercial = base + delta), so
    # the scorecard's critical_fields/.gate rows agree with the routingDecision and
    # reviewReasons the Function actually returned.
    critical = active_critical_fields(response)
    payment_due_date_gate = (
        "critical for this category"
        if "payment_due_date" in critical
        else "not critical - Logic App defaults to invoice_date + 30 days"
    )

    # Lead block: identity, then bill_type (a classified field, so value/.confidence/
    # .gate), then the decision summary. routing_decision/review_reasons used to sit at
    # the very end; they now lead and are not repeated below.
    pairs: List[Tuple[str, str]] = [
        ("run_id_utc", run_id),
        ("source", source),
        ("input_type", input_type),
    ]
    seen: set = set()

    def emit_field(field_name: str) -> None:
        """Append one field's value/.confidence/.gate rows (plus the description length
        checks), at most once per field, so the lead bill_type and the main loop below
        never emit the same field twice."""
        if field_name in seen:
            return
        seen.add(field_name)

        entry = fields.get(field_name)
        entry = entry if isinstance(entry, dict) else None
        value = entry.get("value") if entry else None
        confidence = entry.get("confidence") if entry else None

        pairs.append((field_name, scorecard.csv_scalar(value)))
        pairs.append(
            (f"{field_name}.confidence", "MISSING" if confidence is None else f"{float(confidence):.3f}")
        )
        # For the twin-resolved finals the authoritative pass/fail lives in the resolutions
        # map: a below-threshold field can still pass via the extract+generate agreement
        # rule, so a confidence-derived gate would wrongly show REVIEW. Prefer the resolution
        # and surface which source produced the value.
        resolutions = response.get("resolutions") or {}
        resolution = resolutions.get(field_name) if isinstance(resolutions, dict) else None
        resolution = resolution if isinstance(resolution, dict) else None
        if resolution is not None and resolution.get("passed") is True:
            gate = "pass"
        else:
            gate = field_gate_from_summary(field_name, entry, threshold, critical)
        pairs.append((f"{field_name}.gate", gate))
        if resolution is not None:
            pairs.append((f"{field_name}.source", str(resolution.get("source") or "")))

        if field_name == "invoice_description":
            pairs.append(("invoice_description.word_count", scorecard.csv_scalar(scorecard.count_words(value))))
            pairs.append(("invoice_description.length_gate", scorecard.invoice_description_gate(value)))

    emit_field("bill_type")
    pairs.append(("routing_decision", str(response.get("routingDecision", ""))))
    pairs.append(("review_reasons", " | ".join(response.get("reviewReasons") or [])))

    # Field rows: the requested fields first, in the requested order, then the rest of
    # FIELD_PRINT_ORDER, then any extra fields the Function returned. bill_type is
    # already emitted above; emit_field's `seen` guard keeps every block unique.
    lead_fields = [
        "vendor_name",
        "vendor_name_extract",
        "vendor_name_generate",
        "service_address",
        "service_address_extract",
        "service_address_generate",
        "invoice_date",
        "payment_due_date",
        "invoice_number",
        "po_or_job_number",
        "po_or_job_number_extract",
        "po_or_job_number_generate",
        "gst_amount",
        "gst_amount_extract",
        "gst_amount_generate",
        "total_invoice_amount",
        "total_invoice_amount_extract",
        "total_invoice_amount_generate",
        "is_handwritten",
        "invoice_description",
    ]
    ordered_field_names = (
        lead_fields
        + [n for n in FIELD_PRINT_ORDER if n not in lead_fields]
        + [n for n in fields.keys() if n not in FIELD_PRINT_ORDER and n not in lead_fields]
    )
    for field_name in ordered_field_names:
        emit_field(field_name)

    # Router / analyzer metadata follows the fields, since the requested sequence leads
    # and it can no longer sit at the top.
    pairs.extend(
        [
            ("router_category", str(response.get("routerCategory", ""))),
            ("router_category_path", str(response.get("routerCategoryPath", ""))),
            ("router_confidence_gate", "skipped - not consistently available in CU router result"),
            ("effective_document_type", str(response.get("effectiveDocumentType", ""))),
            ("analyzer_used", str(response.get("analyzerUsed", ""))),
            ("policy_bucket", str(response.get("policyBucket", ""))),
            ("critical_fields", ", ".join(critical)),
            ("payment_due_date_gate", payment_due_date_gate),
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
    src.add_argument(
        "--folder",
        help=(
            "Local folder: score every top-level *.pdf in it as one scorecard column each "
            "(no recursion into subfolders). Each file gets its own random sourceId and a "
            "filename column header, so --source-id/--run-label do not apply; --append-scorecard "
            "appends the whole batch to the existing scorecard."
        ),
    )

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
    ap.add_argument("--scorecard", default=DEFAULT_SCORECARD, help=f"Scorecard HTML filename. Default: {DEFAULT_SCORECARD}")
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


def run_source(
    args: argparse.Namespace,
    *,
    url: str,
    scorecard_path: pathlib.Path,
    result_path: pathlib.Path,
    file_path: Optional[pathlib.Path],
    url_arg: Optional[str],
    source_id: str,
    append: bool,
    run_label: Optional[str],
) -> Tuple[bool, str, int]:
    """POST one source (a local file or the URL), persist the decision JSON, and
    write/append one scorecard column.

    Returns (wrote_column, status_label, exit_code):
      - wrote_column is True only when a scorecard column was actually written (False on
        a non-JSON response, an A1 skip, or a no-fields decision), so a folder batch can
        decide fresh-vs-append and summarise per file.
      - exit_code mirrors the original single-run main() semantics for the --file/--url path.
    """
    body: Dict[str, Any] = {
        "sourceId": source_id,
        "reprocess": args.reprocess,
        "fieldThreshold": args.field_threshold,
    }
    if args.include_raw:
        body["includeRaw"] = True

    if file_path is not None:
        input_type = "file"
        body["fileName"] = file_path.name
        body["contentBase64"] = base64.b64encode(file_path.read_bytes()).decode("ascii")
        source = str(file_path)
    else:
        input_type = "url"
        body["url"] = url_arg
        source = url_arg

    print(f"source id: {source_id}")
    print(f"field threshold: {args.field_threshold:.2f}")
    status, payload = post_invoice(url, body)
    print(f"HTTP {status}")

    # Parse the response. If it is not JSON (e.g. an HTML 500), persist the raw text
    # for debugging and stop - there is nothing to score.
    try:
        response: Dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(payload, encoding="utf-8")
        print(payload)
        print(f"\nNon-JSON response saved to {result_path}; no scorecard written.")
        return False, f"non-JSON (HTTP {status})", 1

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
        return False, "skipped (A1)", 0

    if "fields" not in response:
        # An error decision (e.g. 502 CU failure, 400 bad request): no extraction.
        decision = response.get("routingDecision") or response.get("error")
        print(f"\nResponse has no 'fields' (decision: {decision}); no scorecard column written.")
        return False, f"no fields ({decision})", 0 if status == 200 else 1

    pairs, run_id = build_scorecard_pairs(response, source, input_type, args.field_threshold)
    column_header = scorecard.derive_column_header(run_label, run_id)
    scorecard.write_scorecard_html(scorecard_path, pairs, column_header, append=append)
    print(
        f"Scorecard written to {scorecard_path}"
        + (" (new column)" if append else " (fresh table)")
        + f" - column '{column_header}'"
    )
    return True, "scored", 0


def run_folder(
    args: argparse.Namespace,
    *,
    url: str,
    scorecard_path: pathlib.Path,
    result_path: pathlib.Path,
) -> int:
    """Score every top-level *.pdf in args.folder as one scorecard column each (no
    recursion). The first column actually written overwrites to a fresh table (unless
    --append-scorecard is set), and the rest append, so the batch forms one contiguous
    block of columns. A failing PDF is recorded and skipped, never fatal."""
    folder = pathlib.Path(args.folder)
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")
    # iterdir() is non-recursive and is_file() excludes subdirectories, so only the
    # folder's own *.pdf files (case-insensitive) are scored, sorted for stable columns.
    pdfs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if not pdfs:
        raise SystemExit(f"No top-level PDF files in {folder} (subfolders are not searched).")

    print(f"folder: {folder}  ({len(pdfs)} PDF{'s' if len(pdfs) != 1 else ''})")

    wrote_any = False
    results: List[Tuple[str, str]] = []
    for pdf in pdfs:
        print(f"\n=== {pdf.name} ===")
        # Overwrite to a fresh table on the first column actually written (unless
        # --append-scorecard); append thereafter. Keyed on wrote_any, not loop index,
        # so an early file that produces no column doesn't leave the rest appending to
        # a stale scorecard.
        append = args.append_scorecard or wrote_any
        try:
            wrote, status, _code = run_source(
                args,
                url=url,
                scorecard_path=scorecard_path,
                result_path=result_path,
                file_path=pdf,
                url_arg=None,
                source_id=str(uuid.uuid4()),
                append=append,
                run_label=pdf.name,
            )
        except urllib.error.URLError as exc:  # e.g. func host down - don't kill the batch
            print(f"[error] {pdf.name}: connection failed: {exc.reason}")
            wrote, status = False, f"connection failed: {exc.reason}"
        wrote_any = wrote_any or wrote
        results.append((pdf.name, status))

    columns = sum(1 for _, status in results if status == "scored")
    print("\nFolder batch summary:")
    for name, status in results:
        print(f"  {name}: {status}")
    if columns:
        print(f"\nScorecard: {scorecard_path}  ({columns} column{'s' if columns != 1 else ''})")
    else:
        print(f"\nNo PDF produced a scored column; {scorecard_path} left unchanged.")
    return 0 if columns == len(pdfs) else 1


def main() -> int:
    args = parse_args()

    if _GATES_SOURCE is None:
        print(
            "[note] could not import functionapp/gates.py; using this script's built-in field policy. "
            "If you have changed CRITICAL_FIELDS/FIELD_PRINT_ORDER in gates.py, run from the repo root so "
            "the scorecard's gate column stays in sync."
        )

    if args.folder and (args.source_id or args.run_label):
        raise SystemExit(
            "--source-id and --run-label cannot be combined with --folder: each PDF gets its "
            "own random sourceId and a filename column header."
        )

    out_dir = pathlib.Path(args.out_dir)
    result_path = out_dir / args.result_json
    scorecard_path = out_dir / args.scorecard
    url = args.endpoint + (f"?code={args.code}" if args.code else "")

    if args.folder:
        return run_folder(args, url=url, scorecard_path=scorecard_path, result_path=result_path)

    file_path = pathlib.Path(args.file) if args.file else None
    if file_path is not None and not file_path.is_file():
        raise SystemExit(f"File not found: {file_path}")

    _wrote, _status, exit_code = run_source(
        args,
        url=url,
        scorecard_path=scorecard_path,
        result_path=result_path,
        file_path=file_path,
        url_arg=args.url,
        source_id=args.source_id or str(uuid.uuid4()),
        append=args.append_scorecard,
        run_label=args.run_label,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
