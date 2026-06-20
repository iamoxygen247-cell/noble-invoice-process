"""
gates.py — prototype routing gates for the Noble invoice decision engine.

This module is the heart of the Azure Function. Every helper here is lifted
verbatim (or lightly adapted to return data instead of printing) from the
validated Phase 2 harness step24_test.py, so the Function's routing decision is
identical to what that harness produced.

It has NO Azure dependencies and imports only the standard library, so it can be
unit-tested offline without the Function host or any SDK installed.

Gates implemented here (post-extraction, Stage B):
    B2   router/effective category == other            -> REJECT_B2_OTHER_CATEGORY
    B3   is_handwritten missing/unknown/yes             -> REVIEW_B3_HANDWRITTEN_OR_UNKNOWN
    B4   critical field missing/empty/low-confidence    -> REVIEW_B4_CRITICAL_FIELD
    GST  total - gst does not reconcile to 5%           -> REVIEW_GST_MATH
    B6   line-item row confidence < 0.65                -> advisory only (no decision change)

Gate A1 (item-id idempotency) lives in function_app.py + ledger.py, because it
runs before the Content Understanding call. Gate B1 is intentionally absent: the
GA contentCategories response carries no per-category classifier confidence, so
B4 is the only confidence gate. There is no duplicate-invoice verification in the
pipeline (requirement): no Dataverse duplicate query and no alternate key.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# --- thresholds and field policy (mirrors step24_test.py) --------------------

DEFAULT_FIELD_CONFIDENCE_THRESHOLD = 0.75
GST_RATE = 0.05
GST_TOLERANCE = 0.02
LINE_ITEM_ROW_THRESHOLD = 0.65  # gate B6 (advisory)

CRITICAL_FIELDS = [
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "gst_amount",
    "total_invoice_amount",
    "service_address",
]

# payment_due_date is intentionally not critical (handled by the Logic App).
# po_or_job_number / job_number is intentionally not critical (valid invoices may omit it).
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

ADVISORY_FIELDS = ["vendor_category", "anomaly_flag", "invoice_description"]


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


def fmt_conf(conf: Optional[float]) -> str:
    return "N/A" if conf is None else f"{conf:.3f}"


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


# --- router category detection (category only, no confidence gate) -----------


def find_router_category(full: Dict[str, Any]) -> Tuple[str, str]:
    """
    Return the router-selected category and its JSON path.

    For CU router results the category is commonly at
    $.contents[0].segments[0].category. Router confidence is intentionally not
    evaluated (the GA response does not expose a per-category score).
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

    business_categories = {"general_invoice", "property_tax", "other"}
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
    with fields. The router response yields two content objects: contents[0] is
    the router result (category, no fields); the child result is the one whose
    'fields' is populated.
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


# --- GST reconciliation ------------------------------------------------------


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

    none_result = {
        "amount_excluding_gst_calculated": amount_excluding_gst,
        "expected_gst_5_percent": None,
        "actual_gst": gst,
        "difference": None,
        "gst_math_valid": None,
    }
    if amount_excluding_gst is None or gst is None:
        return none_result
    try:
        actual_gst = round(float(gst), 2)
    except (TypeError, ValueError):
        return none_result

    expected_gst = round(amount_excluding_gst * GST_RATE, 2)
    difference = round(abs(actual_gst - expected_gst), 2)
    return {
        "amount_excluding_gst_calculated": amount_excluding_gst,
        "expected_gst_5_percent": expected_gst,
        "actual_gst": actual_gst,
        "difference": difference,
        "gst_math_valid": difference <= GST_TOLERANCE,
    }


# --- gate B4 (critical field confidence) -------------------------------------


def evaluate_b4(fields: Dict[str, Any], threshold: float) -> Tuple[bool, List[str]]:
    """Non-printing version of step24's print_fields_and_apply_b4."""
    review = False
    reasons: List[str] = []
    for name in CRITICAL_FIELDS:
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
    return review, reasons


# --- gate B6 (line-item row confidence, advisory only) -----------------------


def evaluate_b6(fields: Dict[str, Any], row_threshold: float = LINE_ITEM_ROW_THRESHOLD) -> List[str]:
    """Advisory flags for low-confidence line-item rows. The current general
    invoice schema has no array field, so this is normally inert; it is kept for
    forward compatibility with a future line-items schema."""
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
    Apply the prototype routing gates to a raw Content Understanding result and
    return a JSON-serialisable decision. Mirrors the routing logic in step24's
    main(); priority order is B2 -> B3 -> B4 -> GST -> happy path.
    """
    contents = full.get("contents", [])
    if not isinstance(contents, list):
        contents = []

    router_category, router_category_path = find_router_category(full)
    child_content, child_selection = find_child_content(contents, general_invoice_analyzer_id)

    review_reasons: List[str] = []
    fields: Dict[str, Any] = {}
    category_from_child = "NOT FOUND"
    analyzer_used = "NOT FOUND"

    if child_content:
        category_from_child = str(child_content.get("category", "NOT FOUND"))
        analyzer_used = str(child_content.get("analyzerId", "NOT FOUND"))
        fields = child_content.get("fields", {}) or {}

    display_category = category_from_child if category_from_child != "NOT FOUND" else router_category

    routing_decision = "HAPPY_PATH_CANDIDATE"
    effective_document_type = display_category

    # B2 - category other is a hard reject (no extraction / no auto-write).
    b2_other = str(display_category).lower() == "other" or str(router_category).lower() == "other"
    if b2_other:
        routing_decision = "REJECT_B2_OTHER_CATEGORY"
        effective_document_type = "other"
        review_reasons.append("B2 category is other")

    # No child extraction: cannot run the extraction gates.
    if not child_content:
        if routing_decision == "HAPPY_PATH_CANDIDATE":
            routing_decision = "REVIEW_NO_CHILD_EXTRACTION"
            effective_document_type = "other"
            review_reasons.append("No generalinvoice child analyzer fields found")
        gst = validate_gst_math(fields)
        return _result(routing_decision, effective_document_type, display_category, router_category,
                       router_category_path, analyzer_used, child_selection, fields, gst, review_reasons, [])

    # B3 - handwriting / unknown modality.
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

    # B4 - critical field confidence.
    b4_review, b4_reasons = evaluate_b4(fields, field_threshold)
    if b4_review:
        review_reasons.extend([f"B4 {reason}" for reason in b4_reasons])

    # GST reconciliation.
    gst = validate_gst_math(fields)
    gst_failed = gst.get("gst_math_valid") is False
    if gst_failed:
        review_reasons.append(
            f"GST math failed: difference {gst.get('difference')} > tolerance {GST_TOLERANCE}"
        )

    # B6 - advisory only.
    advisory = evaluate_b6(fields)

    if not b2_other:
        if b3_failed:
            routing_decision = "REVIEW_B3_HANDWRITTEN_OR_UNKNOWN"
        elif b4_review:
            routing_decision = "REVIEW_B4_CRITICAL_FIELD"
        elif gst_failed:
            routing_decision = "REVIEW_GST_MATH"
        else:
            routing_decision = "HAPPY_PATH_CANDIDATE"
            effective_document_type = display_category

    return _result(routing_decision, effective_document_type, display_category, router_category,
                   router_category_path, analyzer_used, child_selection, fields, gst, review_reasons, advisory,
                   is_handwritten_value, is_handwritten_conf)


def _result(routing_decision, effective_document_type, display_category, router_category,
            router_category_path, analyzer_used, child_selection, fields, gst, review_reasons, advisory,
            is_handwritten_value="", is_handwritten_conf=None) -> Dict[str, Any]:
    return {
        "routingDecision": routing_decision,
        "effectiveDocumentType": effective_document_type,
        "category": display_category,
        "routerCategory": router_category,
        "routerCategoryPath": router_category_path,
        "analyzerUsed": analyzer_used,
        "childSelection": child_selection,
        "isHandwritten": is_handwritten_value,
        "isHandwrittenConfidence": is_handwritten_conf,
        "reviewReasons": review_reasons,
        "advisoryFlags": advisory,
        "fields": fields_summary(fields),
        "gst": gst,
        "vendorCategory": get_value(fields.get("vendor_category")),
        "anomalyFlag": get_value(fields.get("anomaly_flag")) or "",
    }
