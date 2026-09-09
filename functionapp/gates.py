"""
gates.py — routing gates for the Noble invoice decision engine.

This module is the heart of the Azure Function's decision. Field-value parsing
helpers are CU-output-aware; the field *rules* (criticality, threshold,
defaulting, derivation) live in ``field_policy.py``. This module imports
``field_policy`` and is otherwise standard-library only, so it remains
unit-testable offline without the Function host or any SDK installed.

Gates implemented here (post-extraction, Stage B):
    B2   router/effective category == other          -> REJECT_B2_OTHER_CATEGORY
         ...unless the caller re-analyzes the document with the general-invoice
         analyzer and apply_b2_rescue() recovers fields, after which the ordinary
         gates judge it (B4 -> review, otherwise happy path)
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
    "folio_number",
    "folio_number_extract",
    "folio_number_generate",
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


# CU represents an absent value as a JSON null, but it sometimes returns the four-character
# STRING "null" instead. Seen since 2026-07-30 on vendor_dba_name_generate -- harmless there,
# because that field is in neither WRITE_FIELDS nor TWIN_FIELDS, so it is never resolved or
# written -- and after the GPT-5.5 upgrade on invoice_number_extract, where it is not harmless.
# A non-empty string is a PRESENT value to every downstream check (is_empty_value below,
# _field_passes and resolve_twin's presence test in field_policy), so it silently defeats the
# municipal invoice-number filename fallback: a clean utility bill that prints no invoice number
# routes to REVIEW_B4_CRITICAL_FIELD instead of auto-writing, carrying the literal "null" in
# writeValues. Measured 14 times across 5 corpus documents, confidence 0.213-0.668 -- always
# below the bar, so it fails the field rather than auto-writing the junk.
#
# So this is a latent defect the model upgrade exposed, not one it created. Normalised here, at
# the single boundary every consumer reads a CU value through, so the rest of the pipeline
# behaves exactly as it does when the model returns a real null -- rather than teaching four
# separate empty-checks about a model quirk. Only this exact token is caught: "nullify" is
# untouched, and so are a genuine 0 / False / 0.0 (see the section header above).
#
# Deliberately just "null": it is the only sentinel string observed across all 3,574 cached
# reads. A new one should surface as a corpus failure, not be silently absorbed.
_CU_NULL_SENTINELS = frozenset({"null"})


def _denull(value: Any) -> Any:
    """A CU value, with the literal string sentinel mapped to a real None."""
    if isinstance(value, str) and value.strip().lower() in _CU_NULL_SENTINELS:
        return None
    return value


def get_value(field_data: Any) -> Any:
    """Return the value from a CU field object without treating 0 or False as missing.
    A literal "null" string is normalised to None -- see _denull."""
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
            return _denull(field_data[key])

    for key, value in field_data.items():
        if key.lower().startswith("value"):
            return _denull(value)

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

    # A billing period whose two EXTRACT twins collapsed onto one date, dropped before either
    # consumer resolves -- both resolutions below and build_write_values re-resolve from this
    # same dict, so a repair applied to only one of them would disagree with the other.
    #
    # It has to happen here rather than being repaired downstream, because by then the damage
    # is done: the collapsed end wins the twin resolution (on merit at 0.921, or through
    # resolve_twin's `elif e_present` tail at 0.415), which blocks the correct generate value
    # -- abbotsford_water's meter reading date at 0.904 -- and the span guard in evaluate_b4
    # then derives the start from the WRONG end, giving 2026-04-01 - 60 = 2026-01-31 instead
    # of 2026-03-01. Dropping both twins hands the field to the generate rescue and the day
    # count, which already produce the right answer on the reads where CU returns nothing.
    collapsed = field_policy.billing_period_extracts_collapsed(
        parsed.get(field_policy.BILLING_START_EXTRACT, (None, None))[0],
        parsed.get(field_policy.BILLING_END_EXTRACT, (None, None))[0],
    )
    if collapsed is not None:
        parsed[field_policy.BILLING_START_EXTRACT] = (None, None)
        parsed[field_policy.BILLING_END_EXTRACT] = (None, None)
        advisory.append(
            f"billing_period extract twins both returned {collapsed}, which is one date and "
            "not a range: discarded, so the period resolves from the generate twin and the "
            "day count"
        )

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

    # Noble books its telecom/cable accounts (Telus, Rogers) as municipal -- a booking
    # policy no classifier can read off the page. The analyzer prompt in fact says the
    # opposite in as many words ("private telecom, internet, and phone companies" are
    # listed as commercial), and both samples were measured classifying 'commercial' on
    # 5 of 5 replicates, so this is decided in code rather than by editing that prompt:
    # a prompt edit would have to contradict an explicit clause, and it perturbs the
    # unrelated fields CU extracts from the same document (same reasoning as
    # demote_non_water_sub_type).
    #
    # Overriding the LABEL here, not the bucket, keeps every consumer consistent:
    # build_write_values re-derives the bucket from `parsed`, so the critical-field set,
    # sub_bill_type, the narrative blanking and the written bill_type all follow from
    # this one assignment. Placed after the twin resolution (the vendor must be resolved)
    # and before build_write_values, its first consumer. The three later municipal-only
    # repairs cannot feed back into it: municipal_payee_override and
    # municipal_name_with_prefix need a printed "City of X", and licence_location_address
    # a 'Locations' table column, none of which a telecom bill has.
    # The resolved vendor AND both raw twins are consulted, because the tiebreak between
    # them is a coin flip this rule must not inherit: 260901_rogers resolves to
    # 'Shaw Cablesystems' (extract, flat 0.785) while its generate twin reads a bare
    # 'Shaw' that reached 0.725 on 1 replicate in 5, and a bare 'Shaw' is deliberately
    # NOT in the allowlist. See field_policy.vendor_bill_type_override.
    vendor_candidates = (
        resolutions[field_policy.VENDOR_FINAL][0],
        parsed.get(field_policy.VENDOR_EXTRACT, (None, None))[0],
        parsed.get(field_policy.VENDOR_GENERATE, (None, None))[0],
    )
    override = field_policy.vendor_bill_type_override(*vendor_candidates)
    if override is not None and override != bill_type_value:
        matched = next(
            v for v in vendor_candidates if field_policy.vendor_bill_type_override(v)
        )
        parsed[field_policy.BILL_TYPE] = (
            override, parsed.get(field_policy.BILL_TYPE, (None, None))[1]
        )
        advisory.append(
            f"bill_type set to {override!r} from the vendor: {matched!r} is one of "
            f"Noble's telecom accounts, which are booked as municipal "
            f"(CU classified it {bill_type_value!r})"
        )
        bill_type_value = override
        bucket = field_policy.resolve_bucket(override)

    write_values, defaulted = field_policy.build_write_values(parsed, field_threshold)

    # bill_type absent from the CU response (defect A9): write the bucket that was actually
    # applied instead of null. `bucket` above already resolved to commercial via the same
    # fail-safe, so this records the decision rather than making one -- it cannot change
    # routing, because the bucket is derived from `parsed` and never re-read from
    # write_values. Placed immediately after build_write_values and BEFORE the B2 and
    # no-child early returns, so every path that emits writeValues gets it -- the same
    # reasoning that moved account formatting into build_write_values. Audited three ways
    # like the invoice_number filename fallback: the write value, defaultedFields, and an
    # advisory, so a substituted label is never mistaken for a read one.
    bill_type_fallback = field_policy.default_bill_type(bill_type_value)
    if bill_type_fallback is not None:
        write_values[field_policy.BILL_TYPE] = bill_type_fallback
        defaulted.append(field_policy.BILL_TYPE)
        advisory.append(
            f"bill_type was absent from the CU response; defaulted to "
            f"{bill_type_fallback!r}, the bucket the pipeline applied"
        )

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

    # B4 - critical fields for the resolved bucket, plus folio_number on a property tax
    # notice. The sub-type comes from write_values, which is the same resolved label the
    # written record carries, so the critical set and the record can never disagree about
    # what kind of bill this is.
    critical = field_policy.critical_fields(
        bucket, write_values.get(field_policy.SUB_BILL_TYPE)
    )

    # PO rescue: when the twins yield no usable value (failed resolution, or a
    # resolved value that breaks the 11/33 8-digit invariant and so is
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
                # (33 service / 11 repair): refresh the resolved sub_bill_type
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
    # An account/folio number echoed into invoice_number. A property tax notice prints no
    # invoice number at all -- property_surrey carries only 'FOLIO/ROLL NUMBER 5244-50502-6',
    # which is already account_number -- yet the generate twin fills the field with it on 2
    # reads in 6, and that invented value then beats the municipal filename fallback. Mirror
    # of the A5 account/PO guard, and placed BEFORE the fallback so the filename can take
    # over once the echo is discarded. Measured by replaying gates.evaluate over every
    # cached read: 2 fires, both on property_surrey and both correct, and 0 false positives
    # in 3,310 reads spanning 33 analyzer versions. A later --force re-roll overwrote those
    # 2 reads, so the live corpus no longer reproduces the echo on demand -- the
    # deterministic coverage is test_invoice_number_that_is_just_the_account.
    if field_policy.invoice_number_echoes_account(
            write_values[field_policy.INVOICE_FINAL],
            write_values[field_policy.ACCOUNT_FINAL],
            collect_markdown(full)):
        echoed_invoice = write_values[field_policy.INVOICE_FINAL]
        resolutions[field_policy.INVOICE_FINAL] = (None, 0.0, False, None, "account_echo_rejected")
        write_values[field_policy.INVOICE_FINAL] = ""
        advisory.append(
            f"invoice_number {echoed_invoice!r} discarded: it is the account/folio number "
            "repeated and the document prints no invoice number"
        )

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
    # A period END the generate twin supplied alone -- no extract value corroborating it --
    # and which is exactly the invoice date is not a billing period: it is the invoice date
    # leaking into the field. recommend_241105_1061 prints no period at all, and on 1 run in
    # 12 the generate twin answered '2024-11-05' (its invoice date) at 0.425 with the extract
    # twin confidently null; the rescue wrote it and the invoice auto-routed
    # HAPPY_PATH_CANDIDATE. Corroborating against the page cannot catch this -- the invoice
    # date IS printed. Only the generate-only source is guarded: bug_260528_0016 and
    # west_van_water legitimately bill through their invoice date and resolve END from the
    # extract twin on 26/26 and 24/24 cached reads respectively.
    end_val, end_conf, _end_passed, end_note, end_source = (
        resolutions[field_policy.BILLING_END_FINAL]
    )
    if (
        end_source == "generate"
        and not is_empty_value(end_val)
        and str(end_val) == str(resolutions[field_policy.INVOICE_DATE_FINAL][0] or "")
    ):
        resolutions[field_policy.BILLING_END_FINAL] = ("", end_conf, False, end_note, "none")
        write_values[field_policy.BILLING_END_FINAL] = ""
        advisory.append(
            "billing_period_end_date discarded: generate-only value equals the invoice date "
            f"({end_val}), so the bill prints no period end"
        )

    start_val, _start_conf, start_passed, _start_note, start_source = (
        resolutions[field_policy.BILLING_START_FINAL]
    )
    # ...but a start date PRINTED on the page is never overwritten by arithmetic, however low
    # its confidence. vancouver_water resolves both extract twins to a confident null on 4 runs
    # in 12; the generate twin then reads the printed "Oct 1, 2025" at 0.51, and the derivation
    # replaced it with 2025-10-07 computed from number_of_days. The count is not wrong -- 117 is
    # the METERED consumption period, which on a water bill legitimately differs from the
    # Oct 1 - Jan 31 billing span (the same asymmetry reconcile_number_of_days documents from
    # the other side). Arithmetic over a metered count cannot beat the date the bill prints.
    start_printed = not is_empty_value(start_val) and field_policy.date_corroborated_in_text(
        start_val, collect_markdown(full)
    ) is not None
    if not start_printed and (
        is_empty_value(start_val) or (not start_passed and start_source != "extract")
    ):
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

    # Number of days reconciled against the period, once both dates are final (the
    # derivation and the range repair above have run). The count is the least reliable of
    # the three fields -- it is the one CU has been seen inventing a "1" for -- so the dates
    # arbitrate it: they supply it when it is missing and correct it when it contradicts
    # them by more than a metered count honestly can. A count that merely differs from the
    # span is KEPT: on a water bill the meter-reading dates are not the period dates, and
    # that printed count is the consumption the utility actually billed, which is what the
    # tenant utility-sharing calculation wants (field_policy.reconcile_number_of_days).
    reconciled = field_policy.reconcile_number_of_days(
        write_values[field_policy.DAYS_FINAL],
        write_values[field_policy.BILLING_START_FINAL],
        write_values[field_policy.BILLING_END_FINAL],
    )
    if reconciled is not None:
        previous = write_values[field_policy.DAYS_FINAL]
        resolutions[field_policy.DAYS_FINAL] = (
            reconciled, 1.0, True, None, "period_derived",
        )
        write_values[field_policy.DAYS_FINAL] = reconciled
        advisory.append(
            f"number_of_days {reconciled} taken from the billing period "
            f"{write_values[field_policy.BILLING_START_FINAL]}.."
            f"{write_values[field_policy.BILLING_END_FINAL]}, "
            f"replacing {previous!r}, which the period contradicts"
        )

    # A PO/job number echoed into account_number. 260629_0024 prints '# 11022266' beside the
    # paying party -- its job number -- and no customer account anywhere, yet the analyzer
    # fills account_number with it on all 31 replicates (bug_260504_0021: 3 in 14). Both are
    # commercial, where account_number is not critical, so the wrong value auto-writes.
    # Discarded only when the digits match the resolved PO *and* the page prints no account
    # label at all -- every corpus document with a genuine account number prints one, which
    # is what keeps a real account that coincides with a PO safe (field_policy).
    #
    # The write value reaching here is LABELLED ('Account No: 11022266' -- see
    # field_policy.format_account_number) on every bill EXCEPT a property tax notice, which
    # writes it bare (user requirement, 2026-09-09 -- field_policy.strip_account_label).
    # account_number_echoes_po strips every non-digit from both sides before comparing, so
    # neither form changes its verdict, and the discard below writes "" rather than a bare
    # label. Any future rule added here that inspects account_number must expect BOTH forms
    # -- compare digits, not strings.
    if field_policy.account_number_echoes_po(
            write_values[field_policy.ACCOUNT_FINAL],
            write_values[field_policy.PO_FINAL],
            collect_markdown(full)):
        echoed = write_values[field_policy.ACCOUNT_FINAL]
        resolutions[field_policy.ACCOUNT_FINAL] = (None, 0.0, False, None, "po_echo_rejected")
        write_values[field_policy.ACCOUNT_FINAL] = ""
        advisory.append(
            f"account_number {echoed!r} discarded: it is the PO/job number repeated and the "
            "document prints no account number"
        )

    # On a property tax notice the folio supplies account_number (build_write_values, per the
    # 2026-09-09 requirement) -- so point the account RESOLUTION at the folio too, and let B4
    # judge the value that is actually written instead of the account twins it replaced.
    #
    # Without this the record is correct and the notice still reviews: CU returns no
    # account_number at all on roughly 1 read in 6 of property_delta and property_west_vancouver
    # (a blank field entry, confidence but no value) while folio_number comes back at
    # 0.875-0.988 from the SAME span on the same read. The gate saw the blank twins, not the
    # folio that had already replaced them, and sent a complete correct record to a human.
    #
    # Not a relaxation of the bar: propertytax_account_from_folio returns None unless the folio
    # resolution itself PASSED, so a missing or weak folio still fails B4 exactly as before.
    # Computed here, after the PO-echo discard above, because that is the last rule that can
    # change account_number -- the gate must judge the final value, not an intermediate one.
    _folio_account = field_policy.propertytax_account_from_folio(
        bucket,
        write_values.get(field_policy.SUB_BILL_TYPE),
        write_values.get(field_policy.FOLIO_FINAL),
        resolutions[field_policy.FOLIO_FINAL][2],
    )
    if _folio_account is not None:
        _account_before = resolutions[field_policy.ACCOUNT_FINAL]
        resolutions[field_policy.ACCOUNT_FINAL] = (
            write_values[field_policy.ACCOUNT_FINAL],
            resolutions[field_policy.FOLIO_FINAL][1],
            True,
            None,
            "propertytax_folio",
        )
        if not _account_before[2]:
            advisory.append(
                "account_number resolution taken from folio_number "
                f"({write_values[field_policy.ACCOUNT_FINAL]!r} at "
                f"{resolutions[field_policy.FOLIO_FINAL][1]:.3f}): the account twins resolved "
                f"{_account_before[0]!r} and did not pass, and on a property tax notice the "
                "folio is the account identifier"
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
    # A commercial vendor whose generate twin truncated the printed name. The twins AGREE by
    # containment, so this cannot collide with the domain tiebreak below, which fires only when
    # they DISAGREE -- the two are mutually exclusive by construction and order does not matter.
    # Commercial only: unscoped this rewrites the municipal gas bills, where the short registry
    # spelling is the wanted one. See field_policy.commercial_printed_vendor_name.
    if bucket == field_policy.COMMERCIAL:
        printed_vendor = field_policy.commercial_printed_vendor_name(
            parsed.get(field_policy.VENDOR_EXTRACT, (None, None))[0],
            parsed.get(field_policy.VENDOR_GENERATE, (None, None))[0],
            write_values[field_policy.VENDOR_FINAL],
        )
        if printed_vendor is not None:
            flat_printed = " ".join(str(printed_vendor).split())
            displaced_short = write_values[field_policy.VENDOR_FINAL]
            resolutions[field_policy.VENDOR_FINAL] = (
                printed_vendor, resolutions[field_policy.VENDOR_FINAL][1], True, None,
                "commercial_printed_name",
            )
            write_values[field_policy.VENDOR_FINAL] = flat_printed
            advisory.append(
                f"vendor_name {flat_printed!r} kept over {displaced_short!r}: the generate twin "
                "dropped two or more words of the name printed on the invoice"
            )

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

    # A municipal letterhead that makes its name locatable twice -- the full "City of Delta"
    # and the bare "Delta" -- lets the extractor take the short parse at ~0.415 where it
    # normally takes the full one at ~0.88. The generate twin slips the same way, so the two
    # AGREE and the agreement boost promotes a sub-threshold pair to a passing resolution:
    # twin disagreement cannot catch a perturbation that moves both twins together. The
    # result is a plausible non-empty string, so no critical-field gate fires and the wrong
    # vendor auto-writes. Measured at 3 reads in 768, all three under the two analyzer
    # versions that edited the warranty prompt (docs/ai/warranty-prompt-retry-plan.md).
    #
    # Municipal only, and the resolved value must be the WHOLE bare place name -- together
    # those keep a commercial "Delta Plumbing Ltd." on a page mentioning "City of Delta"
    # untouched. bill_type is safe to gate on: it held 'municipal' at 0.856 on all three
    # slip reads, so it does not fail in company with vendor_name.
    # ...and the same bill's remittance block can capture the vendor outright. burnaby_water
    # prints "By mail to Burnaby Revenue Services"; on 1 read in 12 both twins take the city's
    # accounts-receivable department from there, agree (one is a tail of the other), and write
    # it. The page also states who the cheque is payable to, which is the biller naming itself.
    # A property tax notice whose issuer is printed only as a logo, so the extract twin took
    # plain text from elsewhere on the page. Gated on the classification, which is independent
    # of the vendor slip, and placed BEFORE the two repairs below so municipal_name_with_prefix
    # still runs after it.
    #
    # It fires in TWO regimes, because the wrong value reaches the write two different ways,
    # and each regime carries the guard that suits it:
    #   * the resolution FAILED -- property_vancouver's extract reads 'Property Tax Office' at
    #     0.666, below the bar, and resolve_twin's `elif e_present` tail writes it anyway.
    #     No confidence test here: there is no trustworthy value to protect, and the printed
    #     municipal name the predicate insists on is better than a field that failed.
    #   * the resolution PASSED but the twins DISAGREE -- property_Vancouver2 reads the same
    #     wrong string at 0.745, which clears the bar, so `elif e_pass` takes it on merit and
    #     it AUTO-WRITES. Gating on failure alone missed this one, which is how it was found.
    #     Here the extract IS credible, so the generate must be at least as confident before
    #     it displaces it (0.778 vs 0.745 on that read). Without that test the rule would
    #     overwrite a confident correct extract -- a 0.95 'City of Surrey' losing to a 0.778
    #     'City of Vancouver' merely because the page happens to print the latter.
    # Twins that AGREE are never touched: agreement is the corroboration this whole pipeline
    # rests on, and overriding it would discard the generate twin's casing normalisation.
    # Measured over all 60 cached propertytax reads: 1 read changes, and 0 reads outside
    # propertytax are even candidates.
    _vendor_passed = resolutions[field_policy.VENDOR_FINAL][2]
    _v_extract, _v_extract_conf = parsed.get(field_policy.VENDOR_EXTRACT, (None, None))
    _v_generate, _v_generate_conf = parsed.get(field_policy.VENDOR_GENERATE, (None, None))
    _vendor_generate_wins = (
        not field_policy.vendor_twins_agree(_v_extract, _v_generate)
        and (_v_generate_conf or 0.0) >= (_v_extract_conf or 0.0)
    )
    if bucket == field_policy.MUNICIPAL and (not _vendor_passed or _vendor_generate_wins):
        bill_label, bill_conf = parsed.get(field_policy.BILL_TYPE, (None, None))
        sub_extract, sub_extract_conf = parsed.get(field_policy.SUB_BILL_TYPE, (None, None))
        sub_generate, sub_generate_conf = parsed.get(
            field_policy.SUB_BILL_TYPE_GENERATE, (None, None)
        )
        if (bill_label == field_policy.MUNICIPAL
                and (bill_conf or 0.0) >= field_threshold
                and sub_extract == "propertytax"
                and (sub_extract_conf or 0.0) >= field_policy.SUB_BILL_TYPE_THRESHOLD
                and sub_generate == "propertytax"
                and (sub_generate_conf or 0.0) >= field_policy.SUB_BILL_TYPE_THRESHOLD):
            rescued = field_policy.propertytax_vendor_rescue(
                parsed.get(field_policy.VENDOR_GENERATE, (None, None))[0],
                collect_markdown(full),
            )
            if rescued is not None:
                displaced_vendor = write_values[field_policy.VENDOR_FINAL]
                resolutions[field_policy.VENDOR_FINAL] = (
                    rescued, resolutions[field_policy.VENDOR_FINAL][1], True, None,
                    "propertytax_vendor_rescue",
                )
                write_values[field_policy.VENDOR_FINAL] = rescued
                advisory.append(
                    f"vendor_name {rescued!r} taken from the generate twin, replacing "
                    f"{displaced_vendor!r}: this is a property tax notice and the municipality "
                    "is printed on the page"
                )

    if bucket == field_policy.MUNICIPAL:
        payee = field_policy.municipal_payee_override(
            write_values[field_policy.VENDOR_FINAL], collect_markdown(full)
        )
        if payee is not None:
            displaced = write_values[field_policy.VENDOR_FINAL]
            resolutions[field_policy.VENDOR_FINAL] = (
                payee, resolutions[field_policy.VENDOR_FINAL][1], True, None, "municipal_payee",
            )
            write_values[field_policy.VENDOR_FINAL] = payee
            advisory.append(
                f"vendor_name {payee!r} taken from the printed payable-to line, replacing "
                f"{displaced!r}, which the bill does not name as the payee"
            )

    if bucket == field_policy.MUNICIPAL:
        printed_name = field_policy.municipal_name_with_prefix(
            write_values[field_policy.VENDOR_FINAL], collect_markdown(full)
        )
        if printed_name is not None:
            previous_name = write_values[field_policy.VENDOR_FINAL]
            resolutions[field_policy.VENDOR_FINAL] = (
                printed_name, resolutions[field_policy.VENDOR_FINAL][1], True, None,
                "municipal_prefix_restored",
            )
            write_values[field_policy.VENDOR_FINAL] = printed_name
            advisory.append(
                f"vendor_name {printed_name!r} restored from the printed municipal name, "
                f"replacing {previous_name!r}, which is only its bare place name"
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

    # Printed-label invoice date. The rescue above grounds a value the generate twin
    # produced; on bug_260528_0016 BOTH twins return nothing on 5 replicates in 14, so there
    # is nothing to ground and build_write_values substitutes today -- filing the invoice
    # under the wrong date, unreviewed, because invoice_date is not critical. Read the date
    # off its own printed label instead (field_policy.find_invoice_date_in_text), which is
    # label-anchored and declines on a page carrying two different labelled dates. A
    # document that prints no issue date at all -- the licence-renewal shape invoice_date's
    # NO_GENERATE_RESCUE exists for -- finds nothing here and keeps defaulting exactly as
    # before. Runs before B4 so the future-date gate judges the rescued value.
    if field_policy.INVOICE_DATE_FINAL in defaulted:
        printed_date = field_policy.find_invoice_date_in_text(collect_markdown(full))
        if printed_date is not None:
            resolutions[field_policy.INVOICE_DATE_FINAL] = (
                printed_date, 1.0, True, None, "printed_label",
            )
            write_values[field_policy.INVOICE_DATE_FINAL] = printed_date
            defaulted.remove(field_policy.INVOICE_DATE_FINAL)
            advisory.append(
                f"invoice_date {printed_date} read from its printed label: both twins "
                "returned nothing and the date would otherwise have defaulted to today"
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
    #   dup    -- A15: the extract twin returns the Bill To address itself, but at a
    #             confidence under the bar, on a document that has no service block. The
    #             "never overwrite a present address" rule then keeps the unconfident copy and
    #             the doc reviews, while an identical, CONFIDENT copy sits unused in
    #             bill_to_address. Measured 1 read in 103 on bug_260629_0012 across 18
    #             analyzer definitions -- and the read where CU found MORE is the one that
    #             reviews, which is backwards. Rescued only when the two values name the same
    #             place, so a genuine service address that merely reads low is still never
    #             overwritten by a different Bill To address; that case keeps reviewing.
    sa_val, sa_conf, sa_passed = resolutions[field_policy.SERVICE_ADDRESS_FINAL][:3]
    sa_is_office = field_policy.is_noble_office_address(sa_val)
    sa_unconfident_duplicate = (
        not sa_passed
        and not is_empty_value(sa_val)
        and not sa_is_office
        and field_policy.address_values_agree(
            sa_val, resolutions[field_policy.BILL_TO_ADDRESS_FINAL][0]
        )
    )
    if is_empty_value(sa_val) or sa_is_office or sa_unconfident_duplicate:
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
            elif sa_unconfident_duplicate:
                advisory.append(
                    "service_address promoted from the Bill To block: the service_address "
                    f"twins returned the same address at {sa_conf:.3f}, below the bar, while "
                    f"bill_to_address reads it at {bt_conf:.3f} (A15)"
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

    # Both twins can dip below the bar together and agree on a DIFFERENT reading of the same
    # address -- the third instance of correlated twin failure in this file. The page settles
    # it two ways, in order of authority.
    #
    # 1. A labelled "FOR SERVICE AT:" cell says which block the document itself calls the
    #    service address. richmond_water prints the street there and the street + city +
    #    postal in a separate mailing block; on 1 read in 12 the pair agreed on the mailing
    #    block and stitched its tail onto the service address.
    service_value = write_values[field_policy.SERVICE_ADDRESS_FINAL]
    if service_value:
        labelled = field_policy.labelled_service_address(collect_markdown(full))
        if labelled and labelled.lower() != str(service_value).strip().lower():
            resolutions[field_policy.SERVICE_ADDRESS_FINAL] = (
                labelled, resolutions[field_policy.SERVICE_ADDRESS_FINAL][1], True, None,
                "service_label",
            )
            write_values[field_policy.SERVICE_ADDRESS_FINAL] = labelled
            advisory.append(
                f"service_address {labelled!r} taken from the labelled service cell, "
                f"replacing {service_value!r}, which is not what the label names"
            )

    # 2. Otherwise, restore a postal code the pair dropped -- but only one printed CONTIGUOUSLY
    #    with the address already written, so nothing is assembled from elsewhere on the page.
    #    warranty_260120_0062 prints one continuous run and both twins stopped short of it.
    service_value = write_values[field_policy.SERVICE_ADDRESS_FINAL]
    if service_value:
        completed = field_policy.address_trailing_postal(service_value, collect_markdown(full))
        if completed is not None:
            resolutions[field_policy.SERVICE_ADDRESS_FINAL] = (
                completed, resolutions[field_policy.SERVICE_ADDRESS_FINAL][1], True, None,
                "postal_completed",
            )
            write_values[field_policy.SERVICE_ADDRESS_FINAL] = completed
            advisory.append(
                f"service_address completed to {completed!r}: the postal code is printed "
                "immediately after the address the twins returned"
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


def apply_b2_rescue(original: Dict[str, Any], rescued: Dict[str, Any]) -> Dict[str, Any]:
    """Combine a B2 reject with a direct re-analysis of the same document.

    A router response categorised ``other`` chains no child analyzer, so the B2
    reject carries no fields and a reviewer must re-key every value by hand. The
    caller re-analyzes with the general-invoice analyzer and passes the second
    decision here.

    Pure: no I/O, no clock. The CU call lives in ``function_app`` so that
    ``evaluate`` stays a pure function of a CU result -- which is what lets
    ``diag.py --replay`` and the whole ``regress.py`` harness run offline.

    The rescue can only improve an outcome: with no recovered fields the original
    reject is returned unchanged.

    A rescued document is then judged by the ordinary gates, exactly like any other
    -- B4 decides, and a clean extraction reaches HAPPY_PATH_CANDIDATE. An earlier
    version forced a review here on the grounds that the router had disagreed, but
    that was measured to protect nothing: B4 already catches the shapes the router
    rejects correctly (a near-blank page fails vendor_name, service_address and
    total_invoice_amount on its own), so the downgrade only demoted documents whose
    fields were all good. Provenance survives instead: routerCategory stays 'other',
    an advisory records the rescue, and the ledger stamps RouterCategory so rescued
    rows remain queryable.
    """
    # 'fields' is never empty -- _result injects every resolved final, null or not
    # -- so it cannot tell a real extraction from an empty one. analyzerUsed is
    # set only when find_child_content actually matched a block carrying fields.
    if not rescued or rescued.get("analyzerUsed", "NOT FOUND") == "NOT FOUND":
        return original

    recovered = sum(
        1 for entry in (rescued.get("fields") or {}).values()
        if isinstance(entry, dict) and entry.get("value") not in (None, "")
    )

    out = dict(rescued)
    # routingDecision is deliberately left as the rescued evaluation produced it.
    # A direct re-analysis carries no router segment and the child block has no
    # 'category' of its own, so evaluate() leaves both of these at "NOT FOUND".
    # effectiveDocumentType stamps the ledger's DocumentType column, where that
    # sentinel is meaningless -- a plain B2 reject stamps "other". The rescue ran
    # the general-invoice analyzer and its fields drove bill_type, so that is what
    # the pipeline effectively treated the document as.
    recovered_category = rescued.get("category")
    if not recovered_category or recovered_category == "NOT FOUND":
        recovered_category = "general_invoice"
    out["category"] = recovered_category
    out["effectiveDocumentType"] = recovered_category
    # Keep the router's verdict visible: the re-analysis bypassed the router, so
    # its own routerCategory is 'NOT FOUND' and would erase why this ran at all.
    out["routerCategory"] = original.get("routerCategory", "")
    out["routerCategoryPath"] = original.get("routerCategoryPath", "")
    # reviewReasons is left untouched: on a B4 review it is one reviewer-facing
    # summary of the failing fields, and on a happy path it is empty. The rescue is
    # not a field failure, so it belongs in advisoryFlags -- the informational
    # channel that never gates.
    out["advisoryFlags"] = list(rescued.get("advisoryFlags") or []) + [
        f"B2 rescue: router said {original.get('routerCategory', 'other')!r}, "
        f"re-analysis with the general-invoice analyzer recovered "
        f"{recovered} field value(s)"
    ]
    return out


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
