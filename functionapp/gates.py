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
         written invoice_date after today            -> REVIEW_B4_CRITICAL_FIELD
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
    "vendor_name_extract",
    "vendor_name_generate",
    "invoice_date",
    "invoice_date_extract",
    "invoice_date_generate",
    "payment_due_date",
    "billing_period_start_date",
    "billing_period_start_date_extract",
    "billing_period_start_date_generate",
    "billing_period_end_date",
    "billing_period_end_date_extract",
    "billing_period_end_date_generate",
    "number_of_days",
    "number_of_days_extract",
    "number_of_days_generate",
    "invoice_number",
    "invoice_number_extract",
    "invoice_number_generate",
    "po_or_job_number",
    "po_or_job_number_extract",
    "po_or_job_number_generate",
    "gst_amount",
    "gst_amount_extract",
    "gst_amount_generate",
    "pst_amount",
    "pst_amount_extract",
    "pst_amount_generate",
    "total_invoice_amount",
    "total_invoice_amount_extract",
    "total_invoice_amount_generate",
    "service_address",
    "service_address_extract",
    "service_address_generate",
    "account_number",
    "account_number_extract",
    "account_number_generate",
    "bill_type",
    "sub_bill_type",
    "sub_bill_type_generate",
    "is_handwritten",
    "is_handwritten_generate",
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


def resolve_is_handwritten(fields: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    """
    Resolve the advisory is_handwritten label from the classify field and its
    generate reasoning twin. Either twin saying yes wins: the flag warns that OCR
    quality may be degraded, so a missed handwritten document is the costly
    direction, and the classify field has confidently mislabelled fully
    handwritten receipt-book pages. Otherwise the classify label is kept, with
    the generate value filling in only when the classify field is empty. A
    response without the generate twin (an older analyzer) resolves exactly as
    the classify field alone did before.
    """
    c_val = (get_value(fields.get("is_handwritten")) or "").strip().lower()
    c_conf = get_confidence(fields.get("is_handwritten"))
    g_val = (get_value(fields.get("is_handwritten_generate")) or "").strip().lower()
    g_conf = get_confidence(fields.get("is_handwritten_generate"))
    yes_confs = [conf for val, conf in ((c_val, c_conf), (g_val, g_conf)) if val == "yes"]
    if yes_confs:
        known = [conf for conf in yes_confs if conf is not None]
        return "yes", (max(known) if known else None)
    if c_val:
        return c_val, c_conf
    return g_val, g_conf


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


def collect_markdown(full: Dict[str, Any]) -> str:
    """Concatenated OCR markdown from every contents[] entry (the analyzer runs
    with returnDetails/enableOcr on). All entries are read because which entry
    carries the markdown in a router+child response is not contractual."""
    contents = full.get("contents", [])
    if not isinstance(contents, list):
        return ""
    return "\n".join(
        c["markdown"] for c in contents
        if isinstance(c, dict) and isinstance(c.get("markdown"), str)
    )


# --- gate B4 (critical field confidence) -------------------------------------

def evaluate_b4(
    fields: Dict[str, Any],
    critical: Tuple[str, ...],
    threshold: float,
    resolutions: Optional[Dict[str, Tuple[Any, float, bool, Optional[str], str]]] = None,
) -> Tuple[bool, List[str], List[str]]:
    """
    Review if any critical field for the resolved bucket is missing, empty, or
    below the confidence threshold. The critical set is supplied by the caller
    (field_policy.critical_fields(bucket)), so this gate carries no bucket logic.

    Returns (review, reasons, failed_fields): ``reasons`` are the per-field
    diagnostics; ``failed_fields`` the failing criticals by final name, in
    critical-set order, for the reviewer-facing summary (b4_summary).
    """
    review = False
    reasons: List[str] = []
    failed: List[str] = []
    parsed: Optional[Dict[str, Tuple[Any, Optional[float]]]] = None
    for name in critical:
        # Twin-resolved fields pass when their resolver says so (the raw extract/generate
        # fields are combined, not checked field-by-field). A resolved value may still fail
        # a format rule (e.g. po_or_job_number must be 8 digits), checked on the result.
        # The caller may pass its precomputed (possibly rescued) resolutions; without
        # them, each twin is resolved from the raw fields as before.
        if name in field_policy.TWIN_FIELDS:
            if resolutions is not None and name in resolutions:
                value, best_conf, passed, _note, _source = resolutions[name]
            else:
                if parsed is None:
                    parsed = parse_fields(fields)
                value, best_conf, passed, _note, _source = field_policy.resolve_field(name, parsed, threshold)
            extract_key, generate_key = field_policy.TWIN_FIELDS[name][:2]
            if not passed:
                review = True
                reasons.append(
                    f"{extract_key}/{generate_key} did not clear {threshold:.2f} (best {best_conf:.3f})"
                )
                failed.append(name)
            else:
                fmt = field_policy.format_violation_reason(name, value)
                if fmt is not None:
                    review = True
                    reasons.append(f"{name} must be {fmt}")
                    failed.append(name)
            continue
        field_data = fields.get(name)
        if field_data is None:
            review = True
            reasons.append(f"{name} is missing")
            failed.append(name)
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
            failed.append(name)
        elif confidence is None:
            review = True
            reasons.append(f"{name} confidence is missing")
            failed.append(name)
        elif confidence < threshold:
            review = True
            reasons.append(f"{name} confidence {confidence:.3f} < {threshold:.2f}")
            failed.append(name)
        else:
            format_hint = field_policy.format_violation_reason(name, value)
            if format_hint is not None:
                review = True
                reasons.append(f"{name} must be {format_hint}")
                failed.append(name)
    return review, reasons, failed


def b4_summary(failed_fields: List[str]) -> str:
    """The single reviewer-facing B4 message: '<field> needs attention', with
    two fields joined by 'and' and three-plus as 'a, b and c need attention'.
    Uniform wording for every failure cause (missing/empty/low-confidence/
    format); the per-field diagnostics live in advisoryFlags."""
    if not failed_fields:
        return ""
    if len(failed_fields) == 1:
        return f"{failed_fields[0]} needs attention"
    return f"{', '.join(failed_fields[:-1])} and {failed_fields[-1]} need attention"


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
    file_name: str = "",
) -> Dict[str, Any]:
    """
    Apply the routing gates to a raw Content Understanding result and return a
    JSON-serialisable decision. Priority: B2 -> (no child) -> B4 -> happy path.
    ``file_name`` is the SharePoint filename from the request; it feeds the
    municipal invoice-number fallback only.
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
    is_handwritten_value, is_handwritten_conf = resolve_is_handwritten(fields)

    advisory = evaluate_b6(fields)
    # Resolved finals (value, effective confidence, passed, note, source) for every twin
    # field, keyed by final name. Threaded into _result so the response exposes each final
    # as its fields.<name> entry plus a slim resolution block -- the single source of truth
    # for the write + gate. Any disagree note is surfaced as an advisory.
    resolutions = {
        name: field_policy.resolve_field(name, parsed, field_threshold)
        for name in field_policy.TWIN_FIELDS
    }
    for _final in resolutions.values():
        if _final[3]:
            advisory.append(_final[3])
    write_values, defaulted = field_policy.build_write_values(parsed, field_threshold)

    # B2 - category 'other' is a hard reject (no auto-write, no extraction trust).
    if str(display_category).lower() == "other" or str(router_category).lower() == "other":
        return _result(
            REJECT_B2_OTHER_CATEGORY, "other", display_category, router_category,
            router_category_path, analyzer_used, child_selection, bucket, bill_type_value,
            fields, write_values, defaulted, ["B2 category is other"], advisory,
            is_handwritten_value, is_handwritten_conf, resolutions,
        )

    # No child extraction: cannot run the critical-field gate.
    if not child_content:
        return _result(
            REVIEW_NO_CHILD_EXTRACTION, "other", display_category, router_category,
            router_category_path, analyzer_used, child_selection, bucket, bill_type_value,
            fields, write_values, defaulted,
            ["No generalinvoice child analyzer fields found"], advisory,
            is_handwritten_value, is_handwritten_conf, resolutions,
        )

    # B4 - critical fields for the resolved bucket.
    critical = field_policy.critical_fields(bucket)

    # PO rescue: when the twins yield no usable value (failed resolution, or a
    # resolved value that breaks the 110/330 8-digit invariant and so is
    # guaranteed wrong), scan the OCR markdown for the number deterministically.
    # Exactly one distinct candidate -> auto-accept with an advisory (source
    # "ocr", confidence 1.0: the value is a pure function of the OCR text).
    # Multiple candidates -> surfaced as a review reason after B4 runs.
    po_candidates: List[str] = []
    if field_policy.PO_FINAL in critical:
        po_value, _po_conf, po_passed, _po_note, _po_source = resolutions[field_policy.PO_FINAL]
        if (not po_passed
                or field_policy.format_violation_reason(field_policy.PO_FINAL, po_value) is not None):
            po_candidates = field_policy.find_po_candidates(collect_markdown(full))
            if len(po_candidates) == 1:
                rescued = po_candidates[0]
                resolutions[field_policy.PO_FINAL] = (rescued, 1.0, True, None, "ocr")
                write_values[field_policy.PO_FINAL] = rescued
                advisory.append(
                    f"po_or_job_number rescued from OCR text: {rescued} "
                    f"(CU twins resolved to {po_value!r})"
                )
                # The rescued PO's prefix determines the commercial sub-type
                # (330 service / 110 repair): refresh the resolved sub_bill_type
                # with the new final PO value.
                sub_value, sub_confidence = parsed.get(field_policy.SUB_BILL_TYPE, (None, None))
                write_values[field_policy.SUB_BILL_TYPE] = field_policy.resolve_sub_bill_type(
                    bucket, sub_value, sub_confidence, rescued,
                    parsed.get(field_policy.SUB_BILL_TYPE_GENERATE, (None, None))[0],
                )

    # Invoice-number filename fallback: a municipal bill whose twins produced
    # nothing (both extract and generate empty) takes the SharePoint filename,
    # extension stripped, as its invoice number. Deterministic like the PO
    # rescue (source "filename", confidence 1.0). A present-but-low-confidence
    # value is never overwritten -- that still routes to review -- and with no
    # usable filename the field fails exactly as before.
    if field_policy.INVOICE_FINAL in critical:
        inv_value, _inv_conf, inv_passed, _inv_note, _inv_source = resolutions[field_policy.INVOICE_FINAL]
        if not inv_passed and is_empty_value(inv_value):
            inv_default = field_policy.invoice_number_default(file_name)
            if inv_default is not None:
                resolutions[field_policy.INVOICE_FINAL] = (inv_default, 1.0, True, None, "filename")
                write_values[field_policy.INVOICE_FINAL] = inv_default
                defaulted.append(field_policy.INVOICE_FINAL)
                advisory.append(
                    f"invoice_number defaulted from the SharePoint filename: {inv_default}"
                )

    # Billing-period start derivation: a utility bill that prints no full start
    # date (a month-only period like 'Mar/Apr 2026', or no period at all) takes
    # start = end - (number_of_days - 1) from the resolved end date and day
    # count. Deterministic date arithmetic like the PO rescue (source
    # "derived"). A printed (extract) start value -- even one below the
    # confidence bar -- is never overwritten, but a below-bar generate-only
    # value is: it has no grounding on the page, and in the observed failure
    # mode it is the period *end* date (range collapse on a shared-year range
    # like 'May 19 - May 31, 2026'). Informational only: never gates routing.
    start_val, _start_conf, start_passed, _start_note, start_source = (
        resolutions[field_policy.BILLING_START_FINAL]
    )
    if is_empty_value(start_val) or (not start_passed and start_source != "extract"):
        end_value, end_conf = resolutions[field_policy.BILLING_END_FINAL][:2]
        days_value, days_conf = resolutions[field_policy.DAYS_FINAL][:2]
        derived = field_policy.derive_billing_period_start(end_value, end_conf, days_value, days_conf)
        if derived is not None:
            derived_start, derived_conf = derived
            resolutions[field_policy.BILLING_START_FINAL] = (derived_start, derived_conf, True, None, "derived")
            write_values[field_policy.BILLING_START_FINAL] = derived_start
            note = (
                "billing_period_start_date derived from billing_period_end_date "
                f"minus number_of_days: {derived_start}"
            )
            if not is_empty_value(start_val):
                note += f" (replacing unverified {start_source} value {start_val!r})"
            advisory.append(note)

    # Corroborated invoice-date rescue: the extract twin intermittently returns nothing
    # on bills that plainly print their date, and a generate-only value is refused by
    # default (NO_GENERATE_RESCUE -- it may be a page print timestamp). When the SAME
    # calendar day is printed in the OCR text in an unambiguous month-name or ISO form,
    # the value is grounded, so accept it rather than substituting today. Runs before B4
    # so the future-date gate below judges the rescued value.
    date_val, date_conf, date_passed, _date_note, date_source = (
        resolutions[field_policy.INVOICE_DATE_FINAL]
    )
    if not date_passed and date_source == "generate":
        corroborated = field_policy.date_corroborated_in_text(date_val, collect_markdown(full))
        if corroborated is not None:
            resolutions[field_policy.INVOICE_DATE_FINAL] = (
                corroborated, date_conf, True, None, "corroborated",
            )
            write_values[field_policy.INVOICE_DATE_FINAL] = corroborated
            if field_policy.INVOICE_DATE_FINAL in defaulted:
                defaulted.remove(field_policy.INVOICE_DATE_FINAL)
            advisory.append(
                f"invoice_date {corroborated} accepted from the generate twin: the date "
                "is printed in the document text"
            )

    b4_review, b4_reasons, b4_failed = evaluate_b4(fields, critical, field_threshold, resolutions)
    if len(po_candidates) > 1:
        b4_reasons.append(
            "po_or_job_number has multiple 8-digit candidates in OCR text: "
            + ", ".join(po_candidates)
        )

    # An invoice dated after today is either a misread or a document that should not
    # be paid yet, so it goes to a human. The check runs on the *written* value, so a
    # defaulted (today) date can never trip it, and the future date is written
    # unchanged -- the reviewer needs to see what the document actually said.
    invoice_date_value = write_values.get(field_policy.INVOICE_DATE_FINAL)
    if field_policy.invoice_date_in_future(invoice_date_value):
        b4_review = True
        b4_reasons.append(f"invoice_date {invoice_date_value} is after today")
        b4_failed.append(field_policy.INVOICE_DATE_FINAL)

    if b4_review:
        # reviewReasons carries ONE reviewer-facing summary naming every failing
        # field; the per-field diagnostics (confidence values, twin keys, format
        # hints, PO candidates) go to advisoryFlags so nothing is lost.
        routing_decision = REVIEW_B4_CRITICAL_FIELD
        review_reasons = [b4_summary(b4_failed)]
        advisory.extend(f"B4 {reason}" for reason in b4_reasons)
    else:
        routing_decision = HAPPY_PATH_CANDIDATE
        review_reasons = []

    return _result(
        routing_decision, display_category, display_category, router_category,
        router_category_path, analyzer_used, child_selection, bucket, bill_type_value,
        fields, write_values, defaulted, review_reasons, advisory,
        is_handwritten_value, is_handwritten_conf, resolutions,
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
    resolutions: Optional[Dict[str, Tuple[Any, float, bool, Optional[str], str]]] = None,
) -> Dict[str, Any]:
    # fields_summary mirrors the raw CU fields (the *_extract / *_generate twins, ...). Each
    # computed final is injected as its fields.<name> entry and surfaced in the `resolutions`
    # map, so the response, the scorecard, and the Dynamics write all read the same resolved
    # value/confidence/decision. value/confidence already live in fields.<name> and any
    # disagree note in advisoryFlags, so `resolutions` carries only what that uniform shape
    # can't: whether the field passed and which twin (extract/agreement/generate) produced it.
    summary = fields_summary(fields)
    resolution_out: Dict[str, Dict[str, Any]] = {}
    for final_name, final in (resolutions or {}).items():
        value, conf, passed, _note, source = final
        summary[final_name] = {"value": value, "confidence": conf}
        resolution_out[final_name] = {"passed": passed, "source": source}

    # Reorder so each synthesized final sorts just before its _extract/_generate rows.
    ordered = {name: summary[name] for name in FIELD_PRINT_ORDER if name in summary}
    for name, entry in summary.items():
        ordered.setdefault(name, entry)

    result: Dict[str, Any] = {
        "routingDecision": routing_decision,
        "effectiveDocumentType": effective_document_type,
        "category": display_category,
        "routerCategory": router_category,
        "routerCategoryPath": router_category_path,
        "analyzerUsed": analyzer_used,
        "childSelection": child_selection,
        "billType": bill_type_value,
        "subBillType": write_values.get(field_policy.SUB_BILL_TYPE),
        "policyBucket": bucket,
        "policyVersion": field_policy.POLICY_VERSION,
        "isHandwritten": is_handwritten_value,
        "isHandwrittenConfidence": is_handwritten_conf,
        "reviewReasons": review_reasons,
        "advisoryFlags": advisory,
        "fields": ordered,
        "resolutions": resolution_out,
        "writeValues": write_values,
        "defaultedFields": defaulted,
        "anomalyFlag": get_value(fields.get("anomaly_flag")) or "",
    }
    return result
