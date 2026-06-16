#!/usr/bin/env python3
r"""
Step 2.4 - Content Understanding router test for Noble invoice prototype.

This script is a prototype validation harness. It is not the production pipeline.

What it does:
- Accepts exactly one invoice input:
    --file path\\to\\invoice.pdf
    --url  https://...pdf?...sas
- Calls the router analyzer, default analyzer_id=invoicerouter.
- Uses begin_analyze_binary for local files and begin_analyze for SAS URL inputs.
- Saves the full raw Content Understanding result JSON; overwrites safely if the file already exists.
- Selects the generalinvoice child analyzer output explicitly.
- Skips router/classifier confidence because the current CU router response may not expose it.
- Applies prototype routing gates:
    B2: router category other -> reject / effective category other
    B3: is_handwritten missing/unknown/yes -> human review
    B4: critical field missing, empty, missing confidence, or confidence < threshold -> human review
    GST math: total_invoice_amount - gst_amount should reconcile to 5% GST -> review if invalid
- Writes a matrix CSV scorecard (column 0 = field labels, one column per run); overwrites to a
  fresh single-run matrix by default, or adds the run as a new rightmost column with
  --append-scorecard (auto-migrating a legacy vertical-section file on first append).

Prerequisites:
    python -m pip install azure-ai-contentunderstanding azure-core

Examples:
    python .\\step24_test_no_router_confidence.py --file ".\\samples\\invoice1.pdf"
    python .\\step24_test_no_router_confidence.py --url "https://...blob.core.windows.net/.../invoice1.pdf?...sas..."

API key:
    For this prototype, you may paste the key into the API_KEY constant below.
    You can also override it with --key or AZURE_CU_KEY.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError


# -----------------------------------------------------------------------------
# Prototype configuration
# -----------------------------------------------------------------------------

ENDPOINT = "https://invoice-processing-dev-resource.services.ai.azure.com/"
# SECURITY: do not paste a live key here. The previously embedded key was exposed and
# must be regenerated in the Foundry portal (Deployments pane). Supply the new key via
# the AZURE_CU_KEY environment variable or --key. ensure_inputs() will stop with a clear
# message if no key is provided.
API_KEY = "9n1Mwhx32kjiipj5F3A62Pell1qcBqRZr1VpTNCuBsbxILzaiGxXJQQJ99CFAC4f1cMXJ3w3AAAAACOGphgz"
API_VERSION = "2025-11-01"
ROUTER_ANALYZER_ID = "invoicerouter"
GENERAL_INVOICE_ANALYZER_ID = "generalinvoice"

# User-approved prototype gate thresholds.
# Field threshold stays aligned to the existing B4 design default.
# Use --field-threshold 0.85 if you want a stricter test run.
FIELD_CONFIDENCE_THRESHOLD = 0.75

GST_RATE = 0.05
GST_TOLERANCE = 0.02

# Critical fields in the updated generalinvoice analyzer.
# payment_due_date is intentionally not critical; the Logic App will handle it.
# job_number / PO reference is intentionally not critical because valid invoices may omit it.
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

PLACEHOLDER_VALUES = {
    "",
    "PASTE_YOUR_API_KEY_HERE",
    "YOUR_API_KEY",
    "<your-key>",
}


# -----------------------------------------------------------------------------
# CLI and setup
# -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 2.4 test for Azure Content Understanding router analyzer."
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--file",
        type=Path,
        help="Local invoice PDF/image/document file. Uses begin_analyze_binary.",
    )
    source.add_argument(
        "--url",
        help="Blob SAS URL for the invoice PDF. Uses begin_analyze with AnalysisInput(url=...).",
    )

    parser.add_argument(
        "--endpoint",
        default=os.getenv("AZURE_CU_ENDPOINT", ENDPOINT),
        help=f"Content Understanding endpoint. Default: {ENDPOINT}",
    )
    parser.add_argument(
        "--key",
        default=os.getenv("AZURE_CU_KEY", API_KEY),
        help="Content Understanding API key. Defaults to AZURE_CU_KEY or API_KEY constant.",
    )
    parser.add_argument(
        "--analyzer-id",
        default=os.getenv("AZURE_CU_ANALYZER_ID", ROUTER_ANALYZER_ID),
        help=f"Router analyzer ID. Default: {ROUTER_ANALYZER_ID}",
    )
    parser.add_argument(
        "--general-invoice-analyzer-id",
        default=os.getenv("AZURE_CU_GENERAL_ANALYZER_ID", GENERAL_INVOICE_ANALYZER_ID),
        help=f"Child analyzer ID to prefer. Default: {GENERAL_INVOICE_ANALYZER_ID}",
    )
    parser.add_argument(
        "--api-version",
        default=API_VERSION,
        help=f"API version. Default: {API_VERSION}",
    )
    parser.add_argument(
        "--field-threshold",
        type=float,
        default=FIELD_CONFIDENCE_THRESHOLD,
        help=f"B4 critical field confidence threshold. Default: {FIELD_CONFIDENCE_THRESHOLD}",
    )
    parser.add_argument(
        "--out",
        default="result.json",
        help="Output file for full raw result JSON. Default: result.json",
    )
    parser.add_argument(
        "--scorecard",
        default="step24_scorecard.csv",
        help="CSV file for vertical field/value test summary. Default: step24_scorecard.csv",
    )
    parser.add_argument(
        "--append-scorecard",
        action="store_true",
        help=(
            "Add this run as a new rightmost column in the scorecard matrix. "
            "If the file is in the legacy vertical-section format, it is migrated to a "
            "matrix on first append (prior runs become their own columns). Default "
            "(without this flag) overwrites to a fresh single-run matrix."
        ),
    )
    parser.add_argument(
        "--run-label",
        default=None,
        help=(
            "Optional human-readable column header for this run in the scorecard matrix "
            "(e.g. baseline_0.75). Defaults to the run's UTC timestamp. Header collisions "
            "get a #N suffix."
        ),
    )
    return parser.parse_args()


def ensure_inputs(args: argparse.Namespace) -> None:
    if args.key in PLACEHOLDER_VALUES:
        raise SystemExit(
            "Missing API key. For the prototype, paste it into API_KEY at the top of this script, "
            "or pass --key, or set AZURE_CU_KEY."
        )

    if args.file:
        if not args.file.exists():
            raise SystemExit(f"Local file not found: {args.file}")
        if not args.file.is_file():
            raise SystemExit(f"Path is not a file: {args.file}")


def normalize_endpoint(endpoint: str) -> str:
    return endpoint.rstrip("/") + "/"


# -----------------------------------------------------------------------------
# Content Understanding calls
# -----------------------------------------------------------------------------


def analyze_input(client: ContentUnderstandingClient, args: argparse.Namespace):
    if args.url:
        print(f"Analyzing SAS URL: {args.url}")
        poller = client.begin_analyze(
            analyzer_id=args.analyzer_id,
            inputs=[AnalysisInput(url=args.url)],
        )
        return poller.result()

    file_path: Path = args.file
    content_type = guess_content_type(file_path)
    print(f"Analyzing local file: {file_path}")
    print(f"Content-Type: {content_type}")
    binary_input = file_path.read_bytes()

    # The SDK can infer content type from the bytes in most cases. If your installed
    # SDK version supports content_type, this argument is accepted; otherwise remove it.
    try:
        poller = client.begin_analyze_binary(
            analyzer_id=args.analyzer_id,
            binary_input=binary_input,
            content_type=content_type,
        )
    except TypeError:
        # Backward-compatible with SDK versions that do not expose content_type.
        poller = client.begin_analyze_binary(
            analyzer_id=args.analyzer_id,
            binary_input=binary_input,
        )
    return poller.result()


def guess_content_type(file_path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(file_path))
    if guessed:
        return guessed
    if file_path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


# -----------------------------------------------------------------------------
# JSON helpers
# -----------------------------------------------------------------------------


def as_dict(result: Any) -> Dict[str, Any]:
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if isinstance(result, dict):
        return result
    try:
        return dict(result)
    except Exception as exc:
        raise TypeError(f"Could not convert result to dict: {exc}") from exc


def get_value(field_data: Any) -> Any:
    """Return the value from a CU field object without treating 0 or False as missing."""
    if not isinstance(field_data, dict):
        return None

    value_keys = (
        "valueString",
        "valueNumber",
        "valueDate",
        "valueTime",
        "valueBoolean",
        "valueInteger",
        "valueArray",
        "valueObject",
        "valueJson",
    )
    for key in value_keys:
        if key in field_data:
            return field_data[key]

    for key, value in field_data.items():
        if key.lower().startswith("value"):
            return value

    return None


def get_confidence(field_data: Any) -> Optional[float]:
    if isinstance(field_data, dict):
        conf = field_data.get("confidence")
        if isinstance(conf, (int, float)):
            return float(conf)
    return None


def fmt_conf(conf: Optional[float]) -> str:
    return "N/A" if conf is None else f"{conf:.3f}"


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def short_json(value: Any, max_len: int = 260) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


# -----------------------------------------------------------------------------
# Router/category detection - category only, no confidence gate
# -----------------------------------------------------------------------------


def find_router_category(full: Dict[str, Any]) -> Tuple[str, str]:
    """
    Return the router-selected category and its JSON path.

    For CU router results, the category is commonly at:
        $.contents[0].segments[0].category

    The script intentionally does not evaluate router confidence because the current
    response may not expose a category confidence score.
    """
    contents = full.get("contents", [])
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            segments = first.get("segments", [])
            if isinstance(segments, list) and segments:
                first_segment = segments[0]
                if isinstance(first_segment, dict) and first_segment.get("category") is not None:
                    return str(first_segment.get("category")), "$.contents[0].segments[0].category"

    # Fallback: first business category found anywhere in the result.
    business_categories = {"general_invoice", "property_tax", "other"}
    found_category, found_path = find_first_category_path(full, business_categories)
    if found_category is not None:
        return found_category, found_path

    return "NOT FOUND", "NOT_FOUND"


def find_first_category_path(obj: Any, business_categories: set[str], path: str = "$") -> Tuple[Optional[str], str]:
    if isinstance(obj, dict):
        if "category" in obj:
            value = str(obj.get("category"))
            if value.lower() in business_categories:
                return value, f"{path}.category"
        for key, value in obj.items():
            found, found_path = find_first_category_path(value, business_categories, f"{path}.{key}")
            if found is not None:
                return found, found_path
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found, found_path = find_first_category_path(value, business_categories, f"{path}[{index}]")
            if found is not None:
                return found, found_path
    return None, "NOT_FOUND"


# -----------------------------------------------------------------------------
# Child content selection
# -----------------------------------------------------------------------------


def find_child_content(
    contents: List[Dict[str, Any]],
    preferred_child_analyzer_id: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Prefer the generalinvoice child analyzer output, not merely the first object with fields.
    """
    for content in contents:
        if (
            isinstance(content, dict)
            and content.get("analyzerId") == preferred_child_analyzer_id
            and isinstance(content.get("fields"), dict)
            and content.get("fields")
        ):
            return content, f"matched analyzerId == {preferred_child_analyzer_id}"

    for content in contents:
        if (
            isinstance(content, dict)
            and content.get("category") == "general_invoice"
            and isinstance(content.get("fields"), dict)
            and content.get("fields")
        ):
            return content, "matched category == general_invoice and fields present"

    for content in contents:
        if isinstance(content, dict) and isinstance(content.get("fields"), dict) and content.get("fields"):
            return content, "fallback: first content object with fields"

    return None, "no child content with fields found"


def print_contents_diagnostics(contents: List[Dict[str, Any]]) -> None:
    print("Content objects returned:")
    for i, content in enumerate(contents):
        print(
            f"contents[{i}]: "
            f"kind={content.get('kind')} "
            f"analyzerId={content.get('analyzerId')} "
            f"category={content.get('category', 'NOT PRESENT')} "
            f"fields_present={bool(content.get('fields'))}"
        )
        segments = content.get("segments", [])
        if isinstance(segments, list):
            for j, segment in enumerate(segments):
                if isinstance(segment, dict):
                    print(
                        f"  segment[{j}]: "
                        f"category={segment.get('category', 'NOT PRESENT')}"
                    )


# -----------------------------------------------------------------------------
# Field printing and gate checks
# -----------------------------------------------------------------------------


def print_array_field(field_name: str, field_data: Dict[str, Any]) -> None:
    items = field_data.get("valueArray", [])
    print(f"  {field_name:<26} {'N/A':<12} {'-':<12} {len(items)} item(s)")

    for index, item in enumerate(items):
        props = item.get("valueObject", {}) if isinstance(item, dict) else {}
        if not isinstance(props, dict):
            print(f"    [{index}] {item}")
            continue

        pairs = []
        for prop_name, prop_data in props.items():
            pairs.append(f"{prop_name}={get_value(prop_data)!r}")
        print(f"    [{index}] " + "  ".join(pairs))


def print_fields_and_apply_b4(
    fields: Dict[str, Any],
    threshold: float,
) -> Tuple[bool, List[str]]:
    review_triggered = False
    review_reasons: List[str] = []
    printed = set()

    print(f"{'Field':<28} {'Confidence':<12} {'Gate B4?':<12} Value")
    print("-" * 110)

    ordered_names = FIELD_PRINT_ORDER + [name for name in fields.keys() if name not in FIELD_PRINT_ORDER]

    for field_name in ordered_names:
        if field_name in printed:
            continue
        printed.add(field_name)

        field_data = fields.get(field_name)
        if field_data is None:
            if field_name in FIELD_PRINT_ORDER:
                gate = "REVIEW" if field_name in CRITICAL_FIELDS else "-"
                if field_name in CRITICAL_FIELDS:
                    review_triggered = True
                    review_reasons.append(f"{field_name} is missing")
                print(f"  {field_name:<26} {'N/A':<12} {gate:<12} <missing>")
            continue

        if not isinstance(field_data, dict):
            print(f"  {field_name:<26} {'N/A':<12} {'-':<12} {field_data}")
            continue

        field_type = field_data.get("type")
        if field_type == "array":
            print_array_field(field_name, field_data)
            continue

        value = get_value(field_data)
        confidence = get_confidence(field_data)

        if field_name == "anomaly_flag":
            display = value if value else "(empty - no anomaly)"
            print(f"  {field_name:<26} {'advisory':<12} {'-':<12} {display}")
            continue

        gate = "-"
        if field_name in CRITICAL_FIELDS:
            if is_empty_value(value):
                gate = "REVIEW"
                review_triggered = True
                review_reasons.append(f"{field_name} is empty")
            elif confidence is None:
                gate = "REVIEW"
                review_triggered = True
                review_reasons.append(f"{field_name} confidence is missing")
            elif confidence < threshold:
                gate = "REVIEW"
                review_triggered = True
                review_reasons.append(f"{field_name} confidence {confidence:.3f} < {threshold:.2f}")
            else:
                gate = "pass"

        print(f"  {field_name:<26} {fmt_conf(confidence):<12} {gate:<12} {value}")

    print("-" * 110)
    return review_triggered, review_reasons


# -----------------------------------------------------------------------------
# GST calculation/validation
# -----------------------------------------------------------------------------


def calculate_amount_excluding_gst(fields: Dict[str, Any]) -> Optional[float]:
    total = get_value(fields.get("total_invoice_amount"))
    gst = get_value(fields.get("gst_amount"))
    if total is None or gst is None:
        return None
    try:
        return round(float(total) - float(gst), 2)
    except (TypeError, ValueError):
        return None


def validate_gst_math(fields: Dict[str, Any]) -> Dict[str, Any]:
    amount_excluding_gst = calculate_amount_excluding_gst(fields)
    gst = get_value(fields.get("gst_amount"))

    if amount_excluding_gst is None or gst is None:
        return {
            "amount_excluding_gst_calculated": amount_excluding_gst,
            "expected_gst_5_percent": None,
            "actual_gst": gst,
            "difference": None,
            "gst_math_valid": None,
        }

    try:
        actual_gst = round(float(gst), 2)
    except (TypeError, ValueError):
        return {
            "amount_excluding_gst_calculated": amount_excluding_gst,
            "expected_gst_5_percent": None,
            "actual_gst": gst,
            "difference": None,
            "gst_math_valid": None,
        }

    expected_gst = round(amount_excluding_gst * GST_RATE, 2)
    difference = round(abs(actual_gst - expected_gst), 2)

    return {
        "amount_excluding_gst_calculated": amount_excluding_gst,
        "expected_gst_5_percent": expected_gst,
        "actual_gst": actual_gst,
        "difference": difference,
        "gst_math_valid": difference <= GST_TOLERANCE,
    }


# -----------------------------------------------------------------------------
# Stable output file helpers
# -----------------------------------------------------------------------------


def run_id_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def temp_path_for(path: Path) -> Path:
    """Return a temp file path in the same directory for atomic replacement."""
    suffix = path.suffix or ".tmp"
    return path.with_name(f".{path.stem}.{os.getpid()}.tmp{suffix}")


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON safely whether or not the target file already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = temp_path_for(path)
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_all_rows(path: Path) -> List[List[str]]:
    """Read every row of a CSV file. Returns [] if the file is missing or empty."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return [row for row in csv.reader(f)]


def write_rows_atomic(path: Path, out_rows: List[List[str]]) -> None:
    """Write all rows to CSV via a temp file plus atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = temp_path_for(path)
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for row in out_rows:
                writer.writerow(row)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def derive_column_header(run_label: Optional[str], run_id: str) -> str:
    """Column header for this run: the run label if given, otherwise the UTC run id."""
    label = (run_label or "").strip()
    return label if label else run_id


def unique_header(candidate: str, existing: List[str]) -> str:
    """Return candidate, suffixed #2, #3, ... if it collides with an existing column."""
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}#{n}" in existing:
        n += 1
    return f"{candidate}#{n}"


def looks_like_old_vertical(rows: List[List[str]]) -> bool:
    """The legacy stacked format is identified by the presence of a 'record_start' row."""
    return any(row and row[0] == "record_start" for row in rows)


def parse_vertical_sections(rows: List[List[str]]) -> List[List[Tuple[str, str]]]:
    """
    Parse the legacy stacked scorecard into one (key, value) list per run section.
    Scaffolding rows (header, record_start, record_end, blank separators) are dropped.
    """
    sections: List[List[Tuple[str, str]]] = []
    current: Optional[List[Tuple[str, str]]] = None
    for row in rows:
        if not row:
            continue
        key = row[0]
        value = row[1] if len(row) > 1 else ""
        if key == "field" and value == "value":
            continue
        if key == "record_start":
            current = []
            continue
        if key == "record_end":
            if current is not None:
                sections.append(current)
                current = None
            continue
        if key == "" and value == "":
            continue
        if current is not None:
            current.append((key, value))
    if current:  # tolerate a trailing section that was never closed with record_end
        sections.append(current)
    return sections


def matrix_from_sections(
    sections: List[List[Tuple[str, str]]],
) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """Convert parsed legacy sections into (headers, ordered_keys, key_to_cells)."""
    headers: List[str] = []
    order: List[str] = []
    seen = set()
    maps: List[Dict[str, str]] = []
    for i, section in enumerate(sections):
        section_map: Dict[str, str] = {}
        for key, value in section:
            section_map[key] = value  # within a section, last value wins
            if key not in seen:
                seen.add(key)
                order.append(key)
        maps.append(section_map)
        raw_header = section_map.get("run_id_utc") or f"run_{i + 1}"
        headers.append(unique_header(raw_header, headers))
    cellmap: Dict[str, List[str]] = {
        key: [maps[i].get(key, "") for i in range(len(sections))] for key in order
    }
    return headers, order, cellmap


def parse_matrix(
    rows: List[List[str]],
) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """Parse an existing matrix scorecard into (headers, ordered_keys, key_to_cells)."""
    if not rows:
        return [], [], {}
    header = rows[0]
    headers = header[1:]
    width = len(headers)
    order: List[str] = []
    cellmap: Dict[str, List[str]] = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        key = row[0]
        cells = row[1:]
        if len(cells) < width:
            cells = cells + [""] * (width - len(cells))
        elif len(cells) > width:
            cells = cells[:width]
        if key in cellmap:
            continue  # defensive: ignore duplicate row labels
        cellmap[key] = cells
        order.append(key)
    return headers, order, cellmap


def write_scorecard_matrix(
    path: Path,
    pairs: List[Tuple[str, str]],
    column_header: str,
    append: bool,
) -> None:
    """
    Write this run as a column in a matrix CSV (column 0 holds the field labels).

    append=False (default): overwrite to a fresh two-column matrix (field + this run).
    append=True: add this run as a new rightmost column. If the existing file is in the
    legacy stacked vertical-section format, it is migrated to a matrix first so prior
    runs are preserved as their own columns.
    """
    # Deduplicate this run's keys defensively; keep first-seen order, last value wins.
    this_keys: List[str] = []
    this_map: Dict[str, str] = {}
    for key, value in pairs:
        if key not in this_map:
            this_keys.append(key)
        this_map[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)

    if append and path.exists() and path.stat().st_size > 0:
        rows = read_all_rows(path)
        if looks_like_old_vertical(rows):
            headers, order, cellmap = matrix_from_sections(parse_vertical_sections(rows))
        else:
            headers, order, cellmap = parse_matrix(rows)

        column_header = unique_header(column_header, headers)
        width = len(headers)

        merged_order = list(order)
        for key in this_keys:
            if key not in cellmap and key not in merged_order:
                merged_order.append(key)

        out_rows: List[List[str]] = [["field"] + headers + [column_header]]
        for key in merged_order:
            left = cellmap.get(key, [""] * width)
            if len(left) < width:
                left = left + [""] * (width - len(left))
            out_rows.append([key] + left + [this_map.get(key, "")])
        write_rows_atomic(path, out_rows)
        return

    # Overwrite (default), or an --append against a missing/empty file: fresh matrix.
    out_rows = [["field", column_header]]
    for key in this_keys:
        out_rows.append([key, this_map[key]])
    write_rows_atomic(path, out_rows)


def count_words(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    return len([word for word in value.replace("/", " ").split() if word.strip()])


def invoice_description_gate(value: Any) -> str:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return "not critical - empty"
    words = count_words(value)
    if words <= 11:
        return "pass"
    return f"warning: {words} words > 11"


# -----------------------------------------------------------------------------
# Scorecard
# -----------------------------------------------------------------------------


def csv_scalar(value: Any) -> str:
    """Serialize a value into one CSV cell."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def field_gate_for_scorecard(field_name: str, field_data: Any, threshold: float) -> str:
    """Return the B4 status for a field in the vertical scorecard."""
    if field_name not in CRITICAL_FIELDS:
        if field_name == "payment_due_date":
            return "not critical - handled by Logic App"
        return "not critical"

    if field_data is None:
        return "REVIEW: missing field"

    value = get_value(field_data)
    confidence = get_confidence(field_data)

    if is_empty_value(value):
        return "REVIEW: empty value"
    if confidence is None:
        return "REVIEW: missing confidence"
    if confidence < threshold:
        return f"REVIEW: confidence {confidence:.3f} < {threshold:.2f}"
    return "pass"


def append_scorecard(
    path: Path,
    source: str,
    input_type: str,
    router_category: str,
    router_category_path: str,
    effective_document_type: str,
    analyzer_used: str,
    fields: Dict[str, Any],
    math_result: Dict[str, Any],
    routing_decision: str,
    review_reasons: List[str],
    field_threshold: float = FIELD_CONFIDENCE_THRESHOLD,
    append: bool = False,
    run_label: Optional[str] = None,
) -> None:
    """
    Write this run's scorecard as a column in a matrix CSV.

    Output shape (column 0 holds the row labels; each run is one column):
        field,<run_a>,<run_b>
        run_id_utc,...,...
        source,...,...
        vendor_name,...,...
        vendor_name.confidence,...,...
        vendor_name.gate,...,...
        ...

    By default this overwrites the target to a fresh single-run matrix. With
    --append-scorecard the run is added as a new rightmost column; a legacy stacked
    file is migrated to a matrix on first append. The column header defaults to the
    run's UTC timestamp, or run_label when provided.
    """
    run_id = run_id_utc()

    pairs: List[Tuple[str, str]] = [
        ("run_id_utc", run_id),
        ("source", source),
        ("input_type", input_type),
        ("router_category", router_category),
        ("router_category_path", router_category_path),
        ("router_confidence_gate", "skipped - not consistently available in CU router result"),
        ("effective_document_type", effective_document_type),
        ("analyzer_used", analyzer_used),
        ("critical_fields", ", ".join(CRITICAL_FIELDS)),
        ("payment_due_date_gate", "not critical - handled by Logic App"),
    ]

    ordered_field_names = FIELD_PRINT_ORDER + [
        name for name in fields.keys() if name not in FIELD_PRINT_ORDER
    ]

    for field_name in ordered_field_names:
        field_data = fields.get(field_name)
        value = get_value(field_data)
        confidence = get_confidence(field_data)

        pairs.append((field_name, csv_scalar(value)))
        pairs.append(
            (f"{field_name}.confidence", "MISSING" if confidence is None else f"{confidence:.3f}")
        )
        pairs.append(
            (f"{field_name}.gate", field_gate_for_scorecard(field_name, field_data, field_threshold))
        )

        if field_name == "invoice_description":
            pairs.append(("invoice_description.word_count", csv_scalar(count_words(value))))
            pairs.append(("invoice_description.length_gate", invoice_description_gate(value)))

    pairs.extend([
        ("amount_excluding_gst_calculated", csv_scalar(math_result.get("amount_excluding_gst_calculated"))),
        ("expected_gst_5_percent", csv_scalar(math_result.get("expected_gst_5_percent"))),
        ("actual_gst", csv_scalar(math_result.get("actual_gst"))),
        ("gst_difference", csv_scalar(math_result.get("difference"))),
        ("gst_math_valid", csv_scalar(math_result.get("gst_math_valid"))),
        ("routing_decision", routing_decision),
        ("review_reasons", " | ".join(review_reasons)),
    ])

    column_header = derive_column_header(run_label, run_id)
    write_scorecard_matrix(path, pairs, column_header, append=append)


# -----------------------------------------------------------------------------
# Main routing simulation
# -----------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    ensure_inputs(args)

    endpoint = normalize_endpoint(args.endpoint)
    input_type = "url" if args.url else "file"
    source = args.url if args.url else str(args.file)

    client = ContentUnderstandingClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(args.key),
        api_version=args.api_version,
    )

    print("=" * 72)
    print("STEP 2.4 CONTENT UNDERSTANDING ROUTER TEST")
    print("=" * 72)
    print(f"Endpoint: {endpoint}")
    print(f"Router analyzer: {args.analyzer_id}")
    print(f"Preferred child analyzer: {args.general_invoice_analyzer_id}")
    print("Router confidence gate: skipped - not consistently available")
    print(f"Result JSON output: {args.out} (overwrite mode)")
    print(f"Scorecard output: {args.scorecard} ({'new column (append)' if args.append_scorecard else 'fresh matrix (overwrite)'})")
    print(f"Critical field threshold: {args.field_threshold:.2f}")
    print(f"Critical fields: {', '.join(CRITICAL_FIELDS)}")
    print("payment_due_date: not critical - handled by Logic App")
    print("=" * 72)

    try:
        result = analyze_input(client, args)
    except AzureError as err:
        message = getattr(err, "message", str(err))
        print(f"[Azure Error]: {message}")
        return 1
    except Exception as err:
        print(f"[Error]: {err}")
        return 1

    full = as_dict(result)

    out_path = Path(args.out)
    write_json_atomic(out_path, full)
    print(f"Full result saved to {out_path}")

    contents = full.get("contents", [])
    if not isinstance(contents, list):
        print("Unexpected result shape: 'contents' is not a list.")
        return 1

    router_category, router_category_path = find_router_category(full)

    child_content, child_selection_reason = find_child_content(
        contents,
        preferred_child_analyzer_id=args.general_invoice_analyzer_id,
    )

    print("=" * 72)
    print("CONTENT SELECTION")
    print("=" * 72)
    print_contents_diagnostics(contents)
    print(f"Child selection: {child_selection_reason}")
    print()

    fields: Dict[str, Any] = {}
    category_from_child = "NOT FOUND"
    analyzer_used = "NOT FOUND"

    if child_content:
        category_from_child = str(child_content.get("category", "NOT FOUND"))
        analyzer_used = str(child_content.get("analyzerId", "NOT FOUND"))
        fields = child_content.get("fields", {}) or {}

    display_category = category_from_child if category_from_child != "NOT FOUND" else router_category

    print("=" * 72)
    print("CLASSIFICATION RESULT")
    print("=" * 72)
    print(f"Router category:        {router_category}")
    print(f"Router category path:   {router_category_path}")
    print("Router confidence gate: skipped")
    print(f"Child category:         {category_from_child}")
    print(f"Analyzer used:          {analyzer_used}")
    print()

    review_reasons: List[str] = []
    routing_decision = "HAPPY_PATH_CANDIDATE"
    effective_document_type = display_category

    # B2 - keep raw CU category separate from business routing. The original design treats
    # category=other as no extraction / no auto-write.
    b2_other = str(display_category).lower() == "other" or str(router_category).lower() == "other"
    if b2_other:
        routing_decision = "REJECT_B2_OTHER_CATEGORY"
        effective_document_type = "other"
        review_reasons.append("B2 category is other")

    # If there is no child content, we can still save raw JSON and scorecard, but extraction gates cannot run.
    if not child_content:
        if routing_decision == "HAPPY_PATH_CANDIDATE":
            routing_decision = "REVIEW_NO_CHILD_EXTRACTION"
            effective_document_type = "other"
            review_reasons.append("No generalinvoice child analyzer fields found")

        math_result = validate_gst_math(fields)
        print("=" * 72)
        print("ROUTING DECISION")
        print("=" * 72)
        print(f"Decision: {routing_decision}")
        print(f"Effective document type: {effective_document_type}")
        for reason in review_reasons:
            print(f"- {reason}")

        append_scorecard(
            path=Path(args.scorecard),
            source=source,
            input_type=input_type,
            router_category=router_category,
            router_category_path=router_category_path,
            effective_document_type=effective_document_type,
            analyzer_used=analyzer_used,
            fields=fields,
            math_result=math_result,
            routing_decision=routing_decision,
            review_reasons=review_reasons,
            field_threshold=args.field_threshold,
            append=args.append_scorecard,
            run_label=args.run_label,
        )
        print(f"Scorecard written to {args.scorecard}" + (" (new column)" if args.append_scorecard else " (fresh matrix)"))
        return 0

    print("=" * 72)
    print("EXTRACTED FIELDS")
    print("=" * 72)
    b4_review, b4_reasons = print_fields_and_apply_b4(fields, args.field_threshold)

    is_handwritten_field = fields.get("is_handwritten", {})
    is_handwritten_value = (get_value(is_handwritten_field) or "").strip().lower()
    is_handwritten_conf = get_confidence(is_handwritten_field)

    b3_failed = False
    if is_handwritten_value == "yes":
        b3_failed = True
        review_reasons.append(f"B3 is_handwritten = yes; confidence {fmt_conf(is_handwritten_conf)}")
    elif is_handwritten_value not in {"yes", "no"}:
        b3_failed = True
        review_reasons.append("B3 is_handwritten is missing or not yes/no")
    elif is_handwritten_conf is None:
        b3_failed = True
        review_reasons.append("B3 is_handwritten confidence is missing")

    if b4_review:
        review_reasons.extend([f"B4 {reason}" for reason in b4_reasons])

    math_result = validate_gst_math(fields)
    print()
    print("Calculated fields / GST validation:")
    print(json.dumps(math_result, indent=2, ensure_ascii=False))

    gst_math_failed = False
    if math_result.get("gst_math_valid") is False:
        gst_math_failed = True
        review_reasons.append(
            f"GST math failed: difference {math_result.get('difference')} > tolerance {GST_TOLERANCE}"
        )

    if not b2_other:
        if b3_failed:
            routing_decision = "REVIEW_B3_HANDWRITTEN_OR_UNKNOWN"
        elif b4_review:
            routing_decision = "REVIEW_B4_CRITICAL_FIELD"
        elif gst_math_failed:
            routing_decision = "REVIEW_GST_MATH"
        else:
            routing_decision = "HAPPY_PATH_CANDIDATE"
            effective_document_type = display_category

    print()
    print("=" * 72)
    print("ROUTING DECISION")
    print("=" * 72)
    print(f"Decision: {routing_decision}")
    print(f"Effective document type: {effective_document_type}")

    if review_reasons:
        print("Review reasons:")
        for reason in review_reasons:
            print(f"- {reason}")
    else:
        print("All tested extraction gates passed.")
        print("This is a happy-path candidate only; B5 Dataverse duplicate check is not performed here.")

    print()
    print(f"vendor_category: {get_value(fields.get('vendor_category')) or 'N/A'}")
    anomaly = get_value(fields.get("anomaly_flag")) or ""
    print(f"anomaly_flag:    {anomaly if anomaly else '(empty)'}")

    append_scorecard(
        path=Path(args.scorecard),
        source=source,
        input_type=input_type,
        router_category=router_category,
        router_category_path=router_category_path,
        effective_document_type=effective_document_type,
        analyzer_used=analyzer_used,
        fields=fields,
        math_result=math_result,
        routing_decision=routing_decision,
        review_reasons=review_reasons,
        field_threshold=args.field_threshold,
        append=args.append_scorecard,
        run_label=args.run_label,
    )
    print()
    print(f"Scorecard written to {args.scorecard}" + (" (new column)" if args.append_scorecard else " (fresh matrix)"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
