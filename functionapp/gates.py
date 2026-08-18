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
    "bill_to_address",
    "bill_to_address_extract",
    "bill_to_address_generate",
    "account_number",
    "account_number_extract",
    "account_number_generate",
    "bill_type",
    "sub_bill_type",
    "sub_bill_type_generate",
    "is_handwritten",
    "is_handwritten_generate",
    "invoice_description",
    "diagnosis_solution",
    "diagnosis_solution_zh_hant",
    "recommendation",
    "recommendation_zh_hant",
    "warranty",
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

    # A city bill CU labelled 'water' but whose OCR text names no water/sewer/
    # stormwater service (only a fireline fee and/or street cleaning) is not a
    # water bill -- demote sub_bill_type to 'other'. Decided here in code, off
    # the OCR text, so the analyzer prompt (and the unrelated fields CU extracts
    # from the same document) stay untouched.
    write_values[field_policy.SUB_BILL_TYPE] = field_policy.demote_non_water_sub_type(
        write_values[field_policy.SUB_BILL_TYPE], collect_markdown(full)
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

    # Implausible billing period. The derivation above only runs on a start that is missing
    # or unreliable, so a CONFIDENT wrong start survives it -- and the observed failure is
    # exactly that: bug_260605_0017 prints 'Service Period: 06/01/26-06/30/26' and CU reads
    # the end correctly (30 cannot be a month) while reading the same-format start as
    # YY/MM/DD, giving 2006-01-26. A 20-year period, both twins agreeing at 0.989, on a
    # document that routes happy -- so it is written unreviewed and feeds the tenant
    # utility-sharing calculation.
    #
    # Judge the pair rather than the value: no bill covers more than a year, so a span that
    # wide (or inverted) is a misread whatever produced it. Then prefer repair over erasure
    # -- re-read the printed range anchored on the end date we already trust -- and blank the
    # start only when nothing corroborates a repair, because a wrong period date corrupts
    # the sharing calculation while a blank one merely leaves it unknown.
    start_val = write_values[field_policy.BILLING_START_FINAL]
    end_val = write_values[field_policy.BILLING_END_FINAL]
    if field_policy.billing_period_span_implausible(start_val, end_val):
        repaired = field_policy.repair_billing_period_start(collect_markdown(full), end_val)
        if repaired is not None:
            resolutions[field_policy.BILLING_START_FINAL] = (
                repaired, 1.0, True, None, "range_repaired",
            )
            write_values[field_policy.BILLING_START_FINAL] = repaired
            advisory.append(
                f"billing_period_start_date {repaired} re-read from the printed date range: "
                f"{start_val!r} would make the period {end_val}, which is not a billing period"
            )
        else:
            # No printed range to anchor on. The other shape that lands here is range
            # collapse -- the start twin returning the period END date (fortisbc 2026-05-27,
            # surrey_water 2026-04-29), which the span guard catches as a zero-length
            # period. The day count survives that failure, so the same arithmetic the empty
            # case uses recovers the real start; only when that is unavailable too does the
            # field go blank.
            derived = field_policy.derive_billing_period_start(
                *resolutions[field_policy.BILLING_END_FINAL][:2],
                *resolutions[field_policy.DAYS_FINAL][:2],
            )
            if derived is not None and not field_policy.billing_period_span_implausible(
                    derived[0], end_val):
                resolutions[field_policy.BILLING_START_FINAL] = (
                    derived[0], derived[1], True, None, "derived",
                )
                write_values[field_policy.BILLING_START_FINAL] = derived[0]
                advisory.append(
                    "billing_period_start_date derived from billing_period_end_date minus "
                    f"number_of_days: {derived[0]} (replacing {start_val!r}, which would "
                    "make the period longer than a year or run backwards)"
                )
            else:
                resolutions[field_policy.BILLING_START_FINAL] = (
                    "", 0.0, False, None, "span_rejected",
                )
                write_values[field_policy.BILLING_START_FINAL] = ""
                advisory.append(
                    f"billing_period_start_date {start_val!r} discarded: it would make the "
                    f"period run to {end_val}, which is not a billing period, and neither a "
                    "printed range nor a day count corroborates a correction"
                )

    # Domain-corroborated vendor rescue. When a vendor's name is printed only as a stylized
    # logo, vendor_name_extract is steered by its own prompt ("read clearly printed text ...
    # rather than a stylized logo") toward whatever plain text sits in the letterhead -- on
    # 260629_0024 that is the dispatch service in the top-right contact block, so the twins
    # disagree and the wrong one is written. Measured at n=12 on the unmodified prod prompt:
    # the correct vendor came out 2/12. The vendor's own web/e-mail domain is printed on the
    # same letterhead, is machine readable where the logo is not, and no field prompt competes
    # over it -- so it breaks the tie deterministically.
    #
    # Deliberately narrow: only when the twins DISAGREE (an agreement already resolved the
    # field) and exactly ONE side matches a printed domain. Neither matching (municipal bills,
    # where "City of Vancouver" never matches "vancouver") or both matching leaves the existing
    # resolution untouched, so this can only ever fire where the field was already unreliable.
    vendor_value, vendor_conf = resolutions[field_policy.VENDOR_FINAL][:2]
    winner = field_policy.vendor_domain_tiebreak(
        parsed.get(field_policy.VENDOR_EXTRACT, (None, None))[0],
        parsed.get(field_policy.VENDOR_GENERATE, (None, None))[0],
        collect_markdown(full),
    )
    if winner is not None:
        flat = " ".join(str(winner).split())
        if flat.lower() != " ".join(str(vendor_value).split()).lower():
            resolutions[field_policy.VENDOR_FINAL] = (
                winner, vendor_conf, True, None, "domain_corroborated",
            )
            write_values[field_policy.VENDOR_FINAL] = flat
            advisory.append(
                f"vendor_name {flat!r} accepted over {vendor_value!r}: the vendor's own "
                "web or e-mail domain printed on the invoice corroborates it and not "
                "the alternative"
            )

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

    # Corroborated payment-due-date rescue. payment_due_date is a lone extract: no twin, so
    # no agreement boost, and CU's confidence on it jitters right across the 0.73 bar --
    # 2026-06-20 at 0.677/0.721/0.722/0.723 and again at 0.9+ on the SAME document. On the
    # sub-bar runs build_write_values substitutes today+30 for a date the bill plainly
    # prints, and since payment_due_date is not critical the doc still routes happy, so the
    # fabricated date reaches Dynamics unseen. BC utility terms are ~22 days, so the +30
    # default lands about a week PAST the real due date every time.
    #
    # Same grounding rule as invoice_date above: accept the read when the same calendar day
    # is printed in the OCR text in an unambiguous month-name or ISO form. This is the safer
    # direction of that precedent -- there it rescues a *generate* value with no span behind
    # it, here the value is an *extract* and corroboration is a second check on top.
    # A document that prints no due date is untouched: CU returns None, which never
    # corroborates, so the field keeps defaulting exactly as before.
    if field_policy.PAYMENT_DUE_FINAL in defaulted:
        due_val = parsed.get(field_policy.PAYMENT_DUE_FINAL, (None, None))[0]
        corroborated_due = field_policy.date_corroborated_in_text(
            due_val, collect_markdown(full)
        )
        if corroborated_due is not None:
            write_values[field_policy.PAYMENT_DUE_FINAL] = corroborated_due
            defaulted.remove(field_policy.PAYMENT_DUE_FINAL)
            advisory.append(
                f"payment_due_date {corroborated_due} kept instead of the +30 default: "
                "the date is printed in the document text"
            )

    # Bill-To fallback for service_address, on two shapes that both leave the field without a
    # serviced property:
    #
    #   empty  -- some small trade/contractor invoices carry no SHIP TO / Service Address /
    #             Attention block at all and address the invoice only to the property
    #             manager's Bill To block, so both service_address twins come back empty.
    #   office -- others print Noble's OWN head office in the SHIP TO block (the manager
    #             receives the paperwork) while the serviced property sits in the SOLD TO
    #             block. The twins then agree confidently on an address that names no
    #             serviced location (samples/bug_260703_0038.pdf: both twins return
    #             'Unit 155 - 13988 Maycrest Way' at 0.74-0.90 on every replicate). The prompt
    #             ranks SHIP TO above SOLD TO, which is right everywhere else, so this is
    #             corrected here rather than by weakening that priority.
    #
    # Either way promote the resolved bill_to_address -- but only when it clears the confidence
    # bar (bt_passed, threshold or twin agreement) and is not itself Noble's head office. A
    # present, non-office service_address is never overwritten, even below threshold: that read
    # found a real address and still routes to review. An office address with no usable Bill To
    # is cleared rather than written, so the doc goes to a human instead of to Dynamics with
    # the paying party's address in it. Runs before B4 so a rescued address prevents the
    # critical-field review route and a cleared one causes it.
    sa_val = resolutions[field_policy.SERVICE_ADDRESS_FINAL][0]
    sa_is_office = field_policy.is_noble_office_address(sa_val)
    if is_empty_value(sa_val) or sa_is_office:
        bt_val, bt_conf, bt_passed, _bt_note, _bt_source = (
            resolutions[field_policy.BILL_TO_ADDRESS_FINAL]
        )
        if bt_passed and not field_policy.is_noble_office_address(bt_val):
            resolutions[field_policy.SERVICE_ADDRESS_FINAL] = (
                bt_val, bt_conf, True, None, "bill_to_fallback",
            )
            write_values[field_policy.SERVICE_ADDRESS_FINAL] = field_policy.normalize_written_text(bt_val)
            if sa_is_office:
                advisory.append(
                    f"service_address replaced from the Bill To block: {bt_val!r} "
                    f"(the SHIP TO block is Noble's own office {sa_val!r}, not a "
                    "serviced property)"
                )
            else:
                advisory.append(
                    f"service_address backfilled from the Bill To block: {bt_val!r} "
                    "(no SHIP TO / Service Address block on the document)"
                )
        elif sa_is_office:
            # Confidence 0.0 so the B4 reason does not report the office read's own high
            # confidence; the advisory carries the real explanation.
            resolutions[field_policy.SERVICE_ADDRESS_FINAL] = (
                None, 0.0, False, None, "noble_office_rejected",
            )
            write_values[field_policy.SERVICE_ADDRESS_FINAL] = None
            advisory.append(
                f"service_address discarded: {sa_val!r} is Noble's own office, not a "
                "serviced property, and no Bill To address could replace it"
            )

    # Span-corroborated service_address. The extract twin intermittently returns a *confident
    # null* (0.78-0.84 -- a confidence in the ABSENCE, not a shaky read) on documents whose
    # address sits outside every labelled block the prompt lists: a line-item job note
    # ('Job# 11024580 | key stuck: 8631 Alexandra Road'), a bare 'Attention:' heading. The
    # generate twin then reads it correctly but reports the weak label evidence as a
    # confidence that straddles the bar (0.41-0.87 on the SAME document), so the identical
    # correct address passes or fails by coin flip -- and the losing runs still WRITE it,
    # then send the doc to review. Measured 28 times across 1,633 cached corpus replicates.
    #
    # Accept it when CU's own spans for that value point at text naming the same place: the
    # span says where it was read, which the invoice_date precedent (date_corroborated_in_text)
    # could not ask for. Narrow on purpose -- only when the extract found NOTHING (source
    # 'generate'), so a disagreeing twin pair keeps its review, and never for Noble's own
    # office. Runs after the Bill To fallback so a cleared office address stays cleared.
    sa_val, sa_conf, sa_passed, _sa_note, sa_source = (
        resolutions[field_policy.SERVICE_ADDRESS_FINAL]
    )
    if (not sa_passed and sa_source == "generate"
            and not field_policy.is_noble_office_address(sa_val)
            and field_policy.address_corroborated_by_span(
                sa_val, fields.get(field_policy.SERVICE_ADDRESS_GENERATE), collect_markdown(full))):
        resolutions[field_policy.SERVICE_ADDRESS_FINAL] = (
            sa_val, sa_conf, True, None, "span_corroborated",
        )
        advisory.append(
            f"service_address {sa_val!r} accepted below the confidence bar: the field's own "
            "span points at that address printed on the document"
        )

    # Licensed-premises fallback for a municipal licence or permit notice. A City of
    # Vancouver business-licence renewal names the serviced property ONLY in a 'Locations'
    # table column -- no SHIP TO, Service Address or Attention block anywhere -- so both
    # twins, which are steered by a list of address-block labels, decline it: on
    # business_license.pdf the extract twin returns nothing on all 10 replicates and the
    # generate twin abstains outright on 1, leaving a base-critical field empty on a
    # document that plainly prints the address. Read the column instead
    # (field_policy.licence_location_address, which confirms the cell is address-shaped).
    #
    # Municipal only, and only once every twin-based path above has failed, so it can fire
    # solely where the field was going to review empty-handed.
    if bucket == field_policy.MUNICIPAL and not resolutions[field_policy.SERVICE_ADDRESS_FINAL][2]:
        premises = field_policy.licence_location_address(collect_markdown(full))
        if premises is not None and not field_policy.is_noble_office_address(premises):
            resolutions[field_policy.SERVICE_ADDRESS_FINAL] = (
                premises, 1.0, True, None, "licence_location",
            )
            write_values[field_policy.SERVICE_ADDRESS_FINAL] = premises
            advisory.append(
                f"service_address {premises!r} read from the licence 'Locations' column: "
                "the notice carries no service-address block"
            )

    # Sectioned-bill GST. A utility bill that splits its charges into sections prints a
    # GST line under each and need not print a bill-level recap
    # (samples/bug_260528_0016.pdf: 0.90 under "Gas charges" + 0.75 under "Other charges
    # & adjustments" on a 34.69 bill). BOTH twins return the first section's line there --
    # 0.90 at 0.517 / 0.461, the confidence signature of picking between equally-labelled
    # candidates -- so they agree, the pair passes on corroboration, and no twin-resolution
    # rule can help. Read the printed GST lines instead and let the bill's own arithmetic
    # confirm the total (field_policy.resolve_sectioned_gst).
    #
    # Both buckets since 2026-08-18. It shipped municipal-only on the theory that sectioned
    # GST is a utility-bill pattern and that commercial shapes legitimately break the 5%
    # identity (a trade invoice whose admin fee is quoted "incl. 5% GST", a multi-invoice
    # statement). recommend_260120_0036 (CentiMark) disproved the first half: it prints a GST
    # row per line item with no TAX SUMMARY recap, and both twins return the first row --
    # 27.50 where 27.50 + 2.97 + 2.15 = 32.62 is the bill's own GST (5% of the 652.30
    # subtotal, and 652.30 + 32.62 = the printed 684.92 total). The second half is handled by
    # the guard, not the bucket: resolve_sectioned_gst only returns a candidate the bill's
    # arithmetic confirms, so an invoice whose GST really does break the identity declines
    # here exactly as it did before.
    #
    # NOT gated on the field being critical, unlike the PO rescue: gst_amount is critical
    # for the commercial bucket only, which is exactly why the observed failure shipped
    # HAPPY_PATH_CANDIDATE on a municipal bill -- with a silently wrong derived
    # amount_excluding_gst -- and never reached a reviewer.
    pst_resolution = resolutions[field_policy.PST_FINAL]
    if (bucket in (field_policy.MUNICIPAL, field_policy.COMMERCIAL)
            # The identity divides by the total, so a total we do not trust could
            # corroborate a wrong candidate. Same for PST: when the pst twins come back
            # empty the field defaults to 0, which is a fact only if nothing was found
            # anywhere (source "none") rather than found and disbelieved.
            and resolutions[field_policy.TOTAL_FINAL][2]
            and (pst_resolution[2] or pst_resolution[4] == "none")):
        gst_value = resolutions[field_policy.GST_FINAL][0]
        summed_gst = field_policy.resolve_sectioned_gst(
            field_policy.find_gst_line_amounts(collect_markdown(full)),
            gst_value,
            write_values[field_policy.TOTAL_FINAL],
            write_values[field_policy.PST_FINAL],
        )
        if summed_gst is not None:
            # Confidence 1.0 and passed like the PO rescue: the value is a pure function
            # of the OCR text and the bill's arithmetic, not a model read.
            resolutions[field_policy.GST_FINAL] = (summed_gst, 1.0, True, None, "sectioned_sum")
            write_values[field_policy.GST_FINAL] = summed_gst
            write_values["amount_excluding_gst"] = field_policy.amount_excluding_gst(
                write_values[field_policy.TOTAL_FINAL], summed_gst
            )
            advisory.append(
                f"gst_amount {summed_gst} taken from the bill's own GST lines: this bill "
                f"taxes each charge section separately and CU returned {gst_value!r}, one "
                "section's line"
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
