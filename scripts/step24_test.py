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
- Saves the full raw Content Understanding result JSON.
- Selects the generalinvoice child analyzer output explicitly.
- Applies prototype routing gates:
    B1: router confidence missing or < 0.80 -> human review, effective category other
    B2: router category other -> human review, effective category other
    B3: is_handwritten missing/unknown/yes -> human review
    B4: critical field missing, empty, missing confidence, or confidence < 0.75 -> human review
    GST math: total_invoice_amount - gst_amount should reconcile to 5% GST -> review if invalid
- Appends a one-row CSV scorecard.

Prerequisites:
    python -m pip install azure-ai-contentunderstanding azure-core

Examples:
    python .\\step24_test_updated.py --file ".\\samples\\invoice1.pdf"
    python .\step24_test_updated.py --url "https://...blob.core.windows.net/.../invoice1.pdf?...sas..."

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
import sys
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
API_KEY = "9n1Mwhx32kjiipj5F3A62Pell1qcBqRZr1VpTNCuBsbxILzaiGxXJQQJ99CFAC4f1cMXJ3w3AAAAACOGphgz"
API_VERSION = "2025-11-01"
ROUTER_ANALYZER_ID = "invoicerouter"
GENERAL_INVOICE_ANALYZER_ID = "generalinvoice"

# User-approved prototype gate thresholds.
ROUTER_CONFIDENCE_THRESHOLD = 0.80
FIELD_CONFIDENCE_THRESHOLD = 0.75

GST_RATE = 0.05
GST_TOLERANCE = 0.02

# Critical fields in the updated generalinvoice analyzer.
# job_number / PO reference is intentionally not critical because valid invoices may omit it.
CRITICAL_FIELDS = [
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "payment_due_date",
    "gst_amount",
    "total_invoice_amount",
    "service_address",
]

FIELD_PRINT_ORDER = [
    "vendor_name",
    "invoice_date",
    "payment_due_date",
    "invoice_number",
    "job_number",
    "po_or_job_number",
    "gst_amount",
    "total_invoice_amount",
    "service_address",
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
        "--router-threshold",
        type=float,
        default=ROUTER_CONFIDENCE_THRESHOLD,
        help=f"B1 router confidence threshold. Default: {ROUTER_CONFIDENCE_THRESHOLD}",
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
        help="CSV file to append one-row test summary. Default: step24_scorecard.csv",
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

    poller = client.begin_analyze_binary(
        analyzer_id=args.analyzer_id,
        binary_input=binary_input,
        content_type=content_type,
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
    # Fallback for Mapping-like Azure SDK objects.
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


def walk_json(obj: Any, path: str = "$") -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk_json(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from walk_json(value, f"{path}[{index}]")
    else:
        yield path, obj


def short_json(value: Any, max_len: int = 260) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


# -----------------------------------------------------------------------------
# Router/category confidence detection
# -----------------------------------------------------------------------------


def find_router_category_candidates(full: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Find dictionaries in the result that appear to carry router/category decisions.

    The exact classifier confidence path is part of Step 2.4 discovery. This function
    is intentionally broad, but the routing decision still fails closed if the selected
    router confidence is missing.
    """
    candidates: List[Dict[str, Any]] = []

    def recurse(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            category_key = first_existing_key(obj, ["category", "contentCategory", "categoryName", "label"])
            if category_key is not None:
                confidence_key = first_numeric_key(
                    obj,
                    [
                        "confidence",
                        "score",
                        "probability",
                        "classificationConfidence",
                        "categoryConfidence",
                    ],
                )
                category_value = obj.get(category_key)
                confidence_value = obj.get(confidence_key) if confidence_key else None

                candidates.append(
                    {
                        "path": path,
                        "category_key": category_key,
                        "category": category_value,
                        "confidence_key": confidence_key,
                        "confidence": float(confidence_value) if isinstance(confidence_value, (int, float)) else None,
                        "analyzerId": obj.get("analyzerId"),
                        "has_fields": bool(obj.get("fields")),
                    }
                )

            for key, value in obj.items():
                recurse(value, f"{path}.{key}")
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                recurse(value, f"{path}[{index}]")

    recurse(full, "$")
    return candidates


def first_existing_key(obj: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for key in keys:
        if key in obj:
            return key
    return None


def first_numeric_key(obj: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for key in keys:
        if isinstance(obj.get(key), (int, float)):
            return key
    return None


def choose_router_category_candidate(
    candidates: List[Dict[str, Any]],
    preferred_child_analyzer_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Choose the best router/category confidence candidate.

    Do not blindly prefer the generalinvoice child content here: child analyzer content
    often has fields and a category but may not carry the router/classifier confidence.
    For Gate B1, prefer business-category candidates that have numeric confidence and
    are not field-bearing child extraction objects.
    """
    del preferred_child_analyzer_id  # Kept in signature for compatibility with prior calls.

    business_categories = {"general_invoice", "other", "property_tax"}

    def is_business_category(candidate: Dict[str, Any]) -> bool:
        return str(candidate.get("category", "")).lower() in business_categories

    # 1. Best: business category, numeric confidence, not the child extraction object.
    for candidate in candidates:
        if (
            is_business_category(candidate)
            and candidate.get("confidence") is not None
            and not candidate.get("has_fields")
        ):
            return candidate

    # 2. Good: business category with numeric confidence anywhere.
    for candidate in candidates:
        if is_business_category(candidate) and candidate.get("confidence") is not None:
            return candidate

    # 3. Any numeric confidence candidate.
    for candidate in candidates:
        if candidate.get("confidence") is not None:
            return candidate

    # 4. Business category without confidence. This will fail closed under B1.
    for candidate in candidates:
        if is_business_category(candidate):
            return candidate

    # 5. Fallback for diagnostics.
    if candidates:
        return candidates[0]

    return None


def print_router_confidence_report(candidates: List[Dict[str, Any]], selected: Optional[Dict[str, Any]]) -> None:
    print()
    print("=" * 72)
    print("STEP 2.4 ROUTER / CLASSIFIER CONFIDENCE CANDIDATES")
    print("=" * 72)

    if not candidates:
        print("No category/classifier candidates found in the raw JSON.")
    else:
        for index, candidate in enumerate(candidates):
            marker = " <-- selected" if selected is candidate else ""
            print(
                f"[{index}] path={candidate['path']} "
                f"category={candidate.get('category')!r} "
                f"confidence={fmt_conf(candidate.get('confidence'))} "
                f"analyzerId={candidate.get('analyzerId')} "
                f"has_fields={candidate.get('has_fields')}" + marker
            )

    print()


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
                        f"category={segment.get('category', 'NOT PRESENT')} "
                        f"confidence={segment.get('confidence', 'NOT PRESENT')}"
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
# Scorecard
# -----------------------------------------------------------------------------


def append_scorecard(
    path: Path,
    source: str,
    input_type: str,
    router_category: str,
    router_confidence: Optional[float],
    router_confidence_path: Optional[str],
    effective_document_type: str,
    analyzer_used: str,
    fields: Dict[str, Any],
    math_result: Dict[str, Any],
    routing_decision: str,
    review_reasons: List[str],
) -> None:
    row = {
        "source": source,
        "input_type": input_type,
        "router_category": router_category,
        "router_confidence": router_confidence,
        "router_confidence_path": router_confidence_path,
        "effective_document_type": effective_document_type,
        "analyzer_used": analyzer_used,
        "vendor_name": get_value(fields.get("vendor_name")),
        "invoice_date": get_value(fields.get("invoice_date")),
        "payment_due_date": get_value(fields.get("payment_due_date")),
        "invoice_number": get_value(fields.get("invoice_number")),
        "job_number": get_value(fields.get("job_number")),
        "po_or_job_number": get_value(fields.get("po_or_job_number")),
        "gst_amount": get_value(fields.get("gst_amount")),
        "total_invoice_amount": get_value(fields.get("total_invoice_amount")),
        "amount_excluding_gst_calculated": math_result.get("amount_excluding_gst_calculated"),
        "gst_math_valid": math_result.get("gst_math_valid"),
        "service_address": get_value(fields.get("service_address")),
        "is_handwritten": get_value(fields.get("is_handwritten")),
        "vendor_category": get_value(fields.get("vendor_category")),
        "anomaly_flag": get_value(fields.get("anomaly_flag")),
        "routing_decision": routing_decision,
        "review_reasons": " | ".join(review_reasons),
    }

    write_header = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


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
    print(f"Router confidence threshold: {args.router_threshold:.2f}")
    print(f"Critical field threshold: {args.field_threshold:.2f}")
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
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)
    print(f"Full result saved to {out_path}")

    contents = full.get("contents", [])
    if not isinstance(contents, list):
        print("Unexpected result shape: 'contents' is not a list.")
        return 1

    router_candidates = find_router_category_candidates(full)
    selected_router_candidate = choose_router_category_candidate(
        router_candidates,
        preferred_child_analyzer_id=args.general_invoice_analyzer_id,
    )
    print_router_confidence_report(router_candidates, selected_router_candidate)

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

    router_category = "NOT FOUND"
    router_confidence: Optional[float] = None
    router_confidence_path: Optional[str] = None
    if selected_router_candidate:
        router_category = str(selected_router_candidate.get("category", "NOT FOUND"))
        router_confidence = selected_router_candidate.get("confidence")
        router_confidence_path = selected_router_candidate.get("path")

    fields: Dict[str, Any] = {}
    category_from_child = "NOT FOUND"
    analyzer_used = "NOT FOUND"

    if child_content:
        category_from_child = str(child_content.get("category", "NOT FOUND"))
        analyzer_used = str(child_content.get("analyzerId", "NOT FOUND"))
        fields = child_content.get("fields", {}) or {}
    else:
        fields = {}

    # Prefer child category when the selected router candidate is only a structural fallback.
    display_category = category_from_child if category_from_child != "NOT FOUND" else router_category

    print("=" * 72)
    print("CLASSIFICATION RESULT")
    print("=" * 72)
    print(f"Router category:        {router_category}")
    print(f"Router confidence:      {fmt_conf(router_confidence)}")
    print(f"Router confidence path: {router_confidence_path or 'NOT FOUND'}")
    print(f"Child category:         {category_from_child}")
    print(f"Analyzer used:          {analyzer_used}")
    print()

    review_reasons: List[str] = []
    routing_decision = "HAPPY_PATH_CANDIDATE"
    effective_document_type = display_category

    # B1 - user-approved stricter rule: missing router confidence also fails closed.
    b1_failed = False
    if router_confidence is None:
        b1_failed = True
        review_reasons.append(
            f"B1 router confidence is missing; threshold is {args.router_threshold:.2f}"
        )
    elif router_confidence < args.router_threshold:
        b1_failed = True
        review_reasons.append(
            f"B1 router confidence {router_confidence:.3f} < {args.router_threshold:.2f}"
        )

    if b1_failed:
        routing_decision = "REVIEW_B1_ROUTER_CONFIDENCE"
        effective_document_type = "other"

    # B2 - user described 'other' as human review category for this prototype.
    if not b1_failed and str(display_category).lower() == "other":
        routing_decision = "REVIEW_B2_OTHER_CATEGORY"
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
        print(f"Effective document type for review queue: {effective_document_type}")
        for reason in review_reasons:
            print(f"- {reason}")

        append_scorecard(
            path=Path(args.scorecard),
            source=source,
            input_type=input_type,
            router_category=router_category,
            router_confidence=router_confidence,
            router_confidence_path=router_confidence_path,
            effective_document_type=effective_document_type,
            analyzer_used=analyzer_used,
            fields=fields,
            math_result=math_result,
            routing_decision=routing_decision,
            review_reasons=review_reasons,
        )
        print(f"Scorecard row appended to {args.scorecard}")
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

    if not b1_failed and str(display_category).lower() != "other":
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
    print(f"Effective document type for review queue: {effective_document_type}")

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
        router_confidence=router_confidence,
        router_confidence_path=router_confidence_path,
        effective_document_type=effective_document_type,
        analyzer_used=analyzer_used,
        fields=fields,
        math_result=math_result,
        routing_decision=routing_decision,
        review_reasons=review_reasons,
    )
    print()
    print(f"Scorecard row appended to {args.scorecard}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
