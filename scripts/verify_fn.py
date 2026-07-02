#!/usr/bin/env python3
r"""
verify_fn.py - exercise the deployed Noble invoice Function in Azure.

stdlib only for its OWN logic (urllib, base64, json); Windows / PowerShell friendly.
A fresh GUID sourceId is generated per run, so gate A1 never short-circuits unless you
pass --source-id yourself. The --insecure flag uses an unverified TLS context, which is
the Python equivalent of curl's --ssl-no-revoke --insecure for a TLS-intercepting
corporate proxy.

This is the deployed-function analogue of local_test.py: besides printing the HTTP
response, --file and --folder now persist an HTML scorecard (same matrix table format as
local_test.py) into an output folder (default .\out):
  - <result-json>   the Function's full decision JSON for the last run (overwritten).
  - <scorecard>     the matrix scorecard, rendered as an HTML table, with one column per
                    run. Supports --append-scorecard and --run-label.
The scorecard machinery is the shared scorecard.py (write_scorecard_html); the row-
building logic is kept local to this script (a deliberate copy of local_test.py's so
verify_fn.py has no dependency on local_test.py). The field policy (critical fields,
print order) is imported from functionapp/gates.py + field_policy.py so the scorecard's
gate column matches what the Function actually decided.

--bad-payload stays stdout-only (there are no extracted fields to score).

Examples (PowerShell):
  python verify_fn.py --base-url "https://<host>" --key "<func-key>" --bad-payload --insecure
  python verify_fn.py --base-url "https://<host>" --key "<func-key>" --file "..\\samples\\invoice1.pdf" --insecure
  python verify_fn.py --base-url "https://<host>" --key "<func-key>" --folder "..\\samples" --insecure
  python verify_fn.py --base-url "https://<host>" --key "<func-key>" --file "..\\samples\\invoice1.pdf" --source-id <guid> --reprocess --insecure

Assumed repo layout (so the imports resolve):
    <repo>\scripts\verify_fn.py       <- this file
    <repo>\scripts\scorecard.py       <- shared matrix module (sibling, REQUIRED)
    <repo>\functionapp\gates.py       <- field policy (FIELD_PRINT_ORDER)
    <repo>\functionapp\field_policy.py<- critical-field policy (critical_fields)
  Run it from the repo root so .\out lands at <repo>\out, or pass --out-dir.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import ssl
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
    # Default the B4 threshold to the Function's own policy value so a default
    # run scores exactly what production decides (the flow sends no override).
    DEFAULT_FIELD_THRESHOLD = float(_field_policy.THRESHOLD)
    _GATES_SOURCE = getattr(_gates, "__file__", "gates")
except Exception:  # ImportError or attribute drift
    DEFAULT_FIELD_THRESHOLD = 0.73  # mirrors field_policy.THRESHOLD (kept in sync)
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

DEFAULT_OUT_DIR = "out"
DEFAULT_RESULT_JSON = "verify_result.json"
DEFAULT_SCORECARD = "verify_scorecard.html"


# -----------------------------------------------------------------------------
# Scorecard reconstruction from the Function response
# (a deliberate copy of local_test.py's, so verify_fn.py stands alone)
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
# HTTP (deployed-function path: x-functions-key header + optional insecure TLS)
# -----------------------------------------------------------------------------


def post_invoice(url: str, body: Dict[str, Any], key: Optional[str], insecure: bool) -> Tuple[int, str]:
    """POST the JSON body to the deployed Function. The function key is sent as the
    x-functions-key header; --insecure skips TLS verification for a TLS-intercepting
    corporate proxy. HTTPError is captured (so a 4xx/5xx is scored like a normal
    response); other URLErrors (connection/TLS failures) propagate to the caller."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if key:
        req.add_header("x-functions-key", key)
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8")


# -----------------------------------------------------------------------------
# Scoring (persist decision JSON + write/append one scorecard column)
# -----------------------------------------------------------------------------


def score_response(
    payload: str,
    status: int,
    *,
    source: str,
    source_id: Optional[str],
    scorecard_path: pathlib.Path,
    result_path: pathlib.Path,
    append: bool,
    run_label: Optional[str],
    field_threshold: float,
) -> Tuple[bool, str, int]:
    """Print the HTTP response (today's stdout dump), persist the decision JSON, and
    write/append one scorecard column.

    Returns (wrote_column, status_label, exit_code):
      - wrote_column is True only when a scorecard column was actually written (False on
        a non-JSON response, an A1 skip, or a no-fields decision), so a folder batch can
        decide fresh-vs-append and summarise per file.
    """
    print("HTTP", status)

    # Parse the response. If it is not JSON (e.g. an HTML 500), persist the raw text for
    # debugging and stop - there is nothing to score.
    try:
        response: Dict[str, Any] = json.loads(payload)
    except ValueError:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(payload, encoding="utf-8")
        print(payload[:4000])
        print(f"\nNon-JSON response saved to {result_path}; no scorecard written.")
        return False, f"non-JSON (HTTP {status})", 1

    # Preserve verify_fn's original stdout dump (truncated JSON + routing summary line).
    print(json.dumps(response, indent=2)[:4000])
    if "routingDecision" in response:
        print(
            "\n--> routingDecision:", response.get("routingDecision"),
            "| status:", response.get("status"),
            "| alreadyProcessed:", response.get("alreadyProcessed"),
        )
        if response.get("ledgerWriteError"):
            print("--> WARNING ledgerWriteError:", response.get("ledgerWriteError"))
    if source_id:
        print("--> sourceId:", source_id)

    # 1) Always persist the Function's decision JSON (the local analogue of result.json).
    scorecard.write_json_atomic(result_path, response)
    print(f"\nResult JSON saved to {result_path} (overwrite mode)")

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

    pairs, run_id = build_scorecard_pairs(response, source, "file", field_threshold)
    column_header = scorecard.derive_column_header(run_label, run_id)
    scorecard.write_scorecard_html(scorecard_path, pairs, column_header, append=append)
    print(
        f"Scorecard written to {scorecard_path}"
        + (" (new column)" if append else " (fresh table)")
        + f" - column '{column_header}'"
    )
    return True, "scored", 0


def run_file(
    args: argparse.Namespace,
    *,
    url: str,
    file_path: pathlib.Path,
    source_id: str,
    append: bool,
    run_label: Optional[str],
    scorecard_path: pathlib.Path,
    result_path: pathlib.Path,
) -> Tuple[bool, str, int]:
    """POST one PDF to the deployed Function, then persist + score the response."""
    body: Dict[str, Any] = {
        "sourceId": source_id,
        "contentBase64": base64.b64encode(file_path.read_bytes()).decode("ascii"),
        "fileName": file_path.name,
        "fieldThreshold": args.field_threshold,
    }
    if args.reprocess:
        body["reprocess"] = True

    print(f"source id: {source_id}")
    print(f"field threshold: {args.field_threshold:.2f}")
    status, payload = post_invoice(url, body, args.key, args.insecure)
    return score_response(
        payload,
        status,
        source=str(file_path),
        source_id=source_id,
        scorecard_path=scorecard_path,
        result_path=result_path,
        append=append,
        run_label=run_label,
        field_threshold=args.field_threshold,
    )


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
            wrote, status, _code = run_file(
                args,
                url=url,
                file_path=pdf,
                source_id=str(uuid.uuid4()),
                append=append,
                run_label=pdf.name,
                scorecard_path=scorecard_path,
                result_path=result_path,
            )
        except urllib.error.URLError as exc:  # e.g. host unreachable - don't kill the batch
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


# -----------------------------------------------------------------------------
# CLI / main
# -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Exercise the deployed Noble invoice Function and record the result.")
    ap.add_argument("--base-url", required=True,
                    help="e.g. https://func-...azurewebsites.net")
    ap.add_argument("--key", help="function key (sent as the x-functions-key header)")

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="path to an invoice PDF")
    src.add_argument(
        "--folder",
        help=(
            "Local folder: score every top-level *.pdf in it as one scorecard column each "
            "(no recursion into subfolders). Each file gets its own random sourceId and a "
            "filename column header, so --source-id/--run-label do not apply; --append-scorecard "
            "appends the whole batch to the existing scorecard."
        ),
    )
    src.add_argument("--bad-payload", action="store_true",
                     help="POST {} to force the HTTP 400 validation path (stdout only, no scorecard)")

    ap.add_argument("--source-id", help="override sourceId (default: a fresh GUID)")
    ap.add_argument("--reprocess", action="store_true", help="bypass gate A1")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (corporate proxy)")
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
    return ap.parse_args()


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

    url = args.base_url.rstrip("/") + "/api/process-invoice"

    # --bad-payload: stdout only, no scorecard (there are no extracted fields to score).
    if args.bad_payload:
        try:
            status, payload = post_invoice(url, {}, args.key, args.insecure)
        except urllib.error.URLError as exc:
            print("REQUEST FAILED:", exc)
            return 2
        print("HTTP", status)
        try:
            print(json.dumps(json.loads(payload), indent=2)[:4000])
        except ValueError:
            print(payload[:4000])
        return 0

    out_dir = pathlib.Path(args.out_dir)
    result_path = out_dir / args.result_json
    scorecard_path = out_dir / args.scorecard

    if args.folder:
        return run_folder(args, url=url, scorecard_path=scorecard_path, result_path=result_path)

    file_path = pathlib.Path(args.file)
    if not file_path.is_file():
        raise SystemExit(f"File not found: {file_path}")

    try:
        _wrote, _status, exit_code = run_file(
            args,
            url=url,
            file_path=file_path,
            source_id=args.source_id or str(uuid.uuid4()),
            append=args.append_scorecard,
            run_label=args.run_label,
            scorecard_path=scorecard_path,
            result_path=result_path,
        )
    except urllib.error.URLError as exc:
        print("REQUEST FAILED:", exc)
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
