"""
gates.py — routing gates for the Noble invoice decision engine.

This module is the heart of the Azure Function's decision. Field-value parsing
helpers are CU-output-aware; the field *rules* (criticality, threshold,
defaulting, derivation) live in ``field_policy.py``. This module imports
``field_policy`` and is otherwise standard-library only, so it remains
unit-testable offline without the Function host or any SDK installed.

Gates implemented here (post-extraction, Stage B):
    B2   router/effective category == other          -> REJECT_B2_OTHER_CATEGORY
    B4   critical field (per bill_type bucket) missing/empty/low-confidence
                                                     -> REVIEW_B4_CRITICAL_FIELD
    (no child fields)                                -> REVIEW_NO_CHILD_EXTRACTION
    B6   line-item row confidence < threshold        -> advisory only

Removed vs. the previous version (by agreement):
    * B3 (handwriting) no longer affects routing. ``is_handwritten`` is extracted
      and surfaced for information only -- a handwritten invoice auto-writes if
      its critical fields clear the threshold.
    * The GST-math reconciliation gate is gone. GST correctness for commercial
      bills is enforced only as critical-field confidence (gst_amount is in the
      commercial delta); arithmetic anomalies are noted advisorily by the
      analyzer's ``anomaly_flag`` generate field.

Gate A1 (item-id idempotency) lives in function_app.py + ledger.py, because it
runs before the Content Understanding call. There is no duplicate-invoice
verification in the pipeline (requirement): no Dataverse duplicate query and no
alternate key.

The policy bucket is read from the classified ``bill_type`` field, never from the
router category and never from the presence/absence of any other field.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import field_policy

# --- routing decisions -------------------------------------------------------

HAPPY_PATH_CANDIDATE = "HAPPY_PATH_CANDIDATE"
REVIEW_B4_CRITICAL_FIELD = "REVIEW_B4_CRITICAL_FIELD"
REJECT_B2_OTHER_CATEGORY = "REJECT_B2_OTHER_CATEGORY"
REVIEW_NO_CHILD_EXTRACTION = "REVIEW_NO_CHILD_EXTRACTION"

LINE_ITEM_ROW_THRESHOLD = 0.65  # gate B6 (advisory)

# Display order for the raw-fields block in the response.
FIELD_PRINT_ORDER = [
    "vendor_name",
    "invoice_date",
    "payment_due_date",
    "invoice_number",
    "po_or_job_number",
    "gst_amount",
    "total_invoice_amount",
    "service_address",
    "bill_type",
    "is_handwritten",
    "invoice_description",
    "anomaly_flag",
]


# --- value / confidence extraction (0 and False are NOT missing) -------------


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


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def parse_fields(fields: Dict[str, Any]) -> Dict[str, Tuple[Any, Optional[float]]]:
    """Flatten CU field objects to {name: (value, confidence)} for field_policy."""
    return {name: (get_value(fd), get_confidence(fd)) for name, fd in fields.items()}


# --- router category detection (category only, no confidence gate) -----------


def find_router_category(full: Dict[str, Any]) -> Tuple[str, str]:
    """
    Return the router-selected category and its JSON path. Router confidence is
    intentionally not evaluated (the GA response exposes no per-category score).
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

    business_categories = {"general_invoice", "other"}
    found_category, found_path = _find_first_category_path(full, business_categories)
    if found_category is not None:
        return found_category, found_path

    return "NOT FOUND", "NOT_FOUND"


def _find_first_category_path(obj: Any, business_categories: set, path: str = "$") -> Tuple[Optional[str], str]:
    if isinstance(obj, dict):
        if "category" in obj:
            value = str(obj.get("category"))
            if value.lower() in business_categories:
                return value, f"{path}.category"
        for key, value in obj.items():
            found, found_path = _find_first_category_path(value, business_categories, f"{path}.{key}")
            if found is not None:
                return found, found_path
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found, found_path = _find_first_category_path(value, business_categories, f"{path}[{index}]")
            if found is not None:
                return found, found_path
    return None, "NOT_FOUND"


# --- child content selection -------------------------------------------------


def find_child_content(
    contents: List[Dict[str, Any]],
    preferred_child_analyzer_id: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Prefer the generalinvoice child analyzer output, not merely the first object
    with fields. contents[0] is the router result (category, no fields); the
    child result is the one whose 'fields' is populated.
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


# --- gate B4 (critical field confidence) -------------------------------------


def evaluate_b4(
    fields: Dict[str, Any],
    critical: Tuple[str, ...],
    threshold: float,
) -> Tuple[bool, List[str]]:
    """
    Review if any critical field for the resolved bucket is missing, empty, or
    below the confidence threshold. The critical set is supplied by the caller
    (field_policy.critical_fields(bucket)), so this gate carries no bucket logic.
    """
    review = False
    reasons: List[str] = []
    for name in critical:
        field_data = fields.get(name)
        if field_data is None:
            review = True
            reasons.append(f"{name} is missing")
            continue
        if not isinstance(field_data, dict):
            continue
        if field_data.get("type") == "array":
            continue
        value = get_value(field_data)
        confidence = get_confidence(field_data)
        if is_empty_value(value):
            review = True
            reasons.append(f"{name} is empty")
        elif confidence is None:
            review = True
            reasons.append(f"{name} confidence is missing")
        elif confidence < threshold:
            review = True
            reasons.append(f"{name} confidence {confidence:.3f} < {threshold:.2f}")
        else:
            format_hint = field_policy.format_violation_reason(name, value)
            if format_hint is not None:
                review = True
                reasons.append(f"{name} must be {format_hint}")
    return review, reasons


# --- gate B6 (line-item row confidence, advisory only) -----------------------


def evaluate_b6(fields: Dict[str, Any], row_threshold: float = LINE_ITEM_ROW_THRESHOLD) -> List[str]:
    """Advisory flags for low-confidence line-item rows. Inert until a line-items
    array field is added to the schema; kept for forward compatibility."""
    flags: List[str] = []
    for name, field_data in fields.items():
        if isinstance(field_data, dict) and field_data.get("type") == "array":
            for index, item in enumerate(field_data.get("valueArray", [])):
                conf = get_confidence(item) if isinstance(item, dict) else None
                if conf is not None and conf < row_threshold:
                    flags.append(f"B6 {name}[{index}] row confidence {conf:.3f} < {row_threshold:.2f}")
    return flags


# --- field summary for the JSON response -------------------------------------


def fields_summary(fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    ordered = FIELD_PRINT_ORDER + [n for n in fields.keys() if n not in FIELD_PRINT_ORDER]
    seen = set()
    for name in ordered:
        if name in seen:
            continue
        seen.add(name)
        field_data = fields.get(name)
        if field_data is None:
            continue
        out[name] = {"value": get_value(field_data), "confidence": get_confidence(field_data)}
    return out


# --- the evaluator -----------------------------------------------------------


def evaluate(
    full: Dict[str, Any],
    field_threshold: float,
    general_invoice_analyzer_id: str = "generalinvoice",
) -> Dict[str, Any]:
    """
    Apply the routing gates to a raw Content Understanding result and return a
    JSON-serialisable decision. Priority: B2 -> (no child) -> B4 -> happy path.
    """
    contents = full.get("contents", [])
    if not isinstance(contents, list):
        contents = []

    router_category, router_category_path = find_router_category(full)
    child_content, child_selection = find_child_content(contents, general_invoice_analyzer_id)

    fields: Dict[str, Any] = {}
    category_from_child = "NOT FOUND"
    analyzer_used = "NOT FOUND"
    if child_content:
        category_from_child = str(child_content.get("category", "NOT FOUND"))
        analyzer_used = str(child_content.get("analyzerId", "NOT FOUND"))
        fields = child_content.get("fields", {}) or {}

    display_category = category_from_child if category_from_child != "NOT FOUND" else router_category

    parsed = parse_fields(fields)
    bill_type_value = parsed.get("bill_type", (None, None))[0]
    bucket = field_policy.resolve_bucket(bill_type_value)

    # is_handwritten is advisory only (B3 retired) -- surfaced, never gates.
    is_handwritten_value = (get_value(fields.get("is_handwritten")) or "").strip().lower()
    is_handwritten_conf = get_confidence(fields.get("is_handwritten"))

    advisory = evaluate_b6(fields)
    write_values, defaulted = field_policy.build_write_values(parsed, field_threshold)

    # B2 - category 'other' is a hard reject (no auto-write, no extraction trust).
    if str(display_category).lower() == "other" or str(router_category).lower() == "other":
        return _result(
            REJECT_B2_OTHER_CATEGORY, "other", display_category, router_category,
            router_category_path, analyzer_used, child_selection, bucket, bill_type_value,
            fields, write_values, defaulted, ["B2 category is other"], advisory,
            is_handwritten_value, is_handwritten_conf,
        )

    # No child extraction: cannot run the critical-field gate.
    if not child_content:
        return _result(
            REVIEW_NO_CHILD_EXTRACTION, "other", display_category, router_category,
            router_category_path, analyzer_used, child_selection, bucket, bill_type_value,
            fields, write_values, defaulted,
            ["No generalinvoice child analyzer fields found"], advisory,
            is_handwritten_value, is_handwritten_conf,
        )

    # B4 - critical fields for the resolved bucket.
    critical = field_policy.critical_fields(bucket)
    b4_review, b4_reasons = evaluate_b4(fields, critical, field_threshold)

    if b4_review:
        routing_decision = REVIEW_B4_CRITICAL_FIELD
        review_reasons = [f"B4 {reason}" for reason in b4_reasons]
    else:
        routing_decision = HAPPY_PATH_CANDIDATE
        review_reasons = []

    return _result(
        routing_decision, display_category, display_category, router_category,
        router_category_path, analyzer_used, child_selection, bucket, bill_type_value,
        fields, write_values, defaulted, review_reasons, advisory,
        is_handwritten_value, is_handwritten_conf,
    )


def _result(
    routing_decision: str,
    effective_document_type: str,
    display_category: str,
    router_category: str,
    router_category_path: str,
    analyzer_used: str,
    child_selection: str,
    bucket: str,
    bill_type_value: Any,
    fields: Dict[str, Any],
    write_values: Dict[str, Any],
    defaulted: List[str],
    review_reasons: List[str],
    advisory: List[str],
    is_handwritten_value: str = "",
    is_handwritten_conf: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "routingDecision": routing_decision,
        "effectiveDocumentType": effective_document_type,
        "category": display_category,
        "routerCategory": router_category,
        "routerCategoryPath": router_category_path,
        "analyzerUsed": analyzer_used,
        "childSelection": child_selection,
        "billType": bill_type_value,
        "policyBucket": bucket,
        "policyVersion": field_policy.POLICY_VERSION,
        "isHandwritten": is_handwritten_value,
        "isHandwrittenConfidence": is_handwritten_conf,
        "reviewReasons": review_reasons,
        "advisoryFlags": advisory,
        "fields": fields_summary(fields),
        "writeValues": write_values,
        "defaultedFields": defaulted,
        "anomalyFlag": get_value(fields.get("anomaly_flag")) or "",
    }
