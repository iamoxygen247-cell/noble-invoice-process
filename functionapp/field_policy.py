"""
field_policy.py — single source of truth for the invoice field rules.

This module owns every bill-type-dependent and value-derivation decision in the
pipeline:

    * the confidence threshold for critical fields (the auto-write bar),
    * which fields are critical for each policy bucket (base + commercial delta),
    * resolving the classified ``bill_type`` label to a policy bucket,
    * the derived "write values" sent to Dynamics: date defaulting (PST,
      normalised to YYYY-MM-DD) and the calculated amount-excluding-GST.

Design rules (kept deliberately, not by default):

    * No Azure dependency and standard-library only, so the whole module is
      unit-testable offline with no Function host or SDK installed. This mirrors
      ``gates.py``'s offline-testable principle.
    * It operates on a *parsed* representation ``{field: (value, confidence)}``,
      not raw Content Understanding JSON. CU-output parsing lives in ``gates.py``;
      this module knows only rules. That keeps the two concerns independent.
    * The bucket is decided from the ``bill_type`` *label* only. It is never
      inferred from the presence or absence of any other field, so changing the
      critical-field policy can never silently change the type determination.
    * The single bucket-dependent surface is ``critical_fields(bucket)``.
      Everything else (threshold, defaulting, derivation) is identical for both
      buckets, so a future third bucket touches only this file.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import PurePath
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# --- constants ---------------------------------------------------------------

POLICY_VERSION = "twin-resolution-v7"

# Critical-field confidence bar (the auto-write threshold). Also used as the
# reliability bar for date defaulting. Single constant => one place to retune.
THRESHOLD = 0.73

# Business timezone for "today" / "today + N" defaulting. West US function host,
# but dates must reflect Noble's local calendar day, so pin to Pacific.
BUSINESS_TZ = ZoneInfo("America/Vancouver")
DUE_DATE_DEFAULT_DAYS = 30
DATE_FORMAT = "%Y-%m-%d"

# Base + delta. The base is verified for every bill regardless of how bill_type
# is classified, so a misclassification can only ever drop a *delta* requirement,
# never a base one. Municipal bills must carry the biller's account number and an
# invoice/licence number; commercial bills capture both informationally only.
BASE_CRITICAL: Tuple[str, ...] = ("vendor_name", "service_address", "total_invoice_amount")
COMMERCIAL_DELTA: Tuple[str, ...] = ("po_or_job_number", "gst_amount")
MUNICIPAL_DELTA: Tuple[str, ...] = ("account_number", "invoice_number")

# Vendor name is captured twice by the analyzer: an extract-method field
# (span-grounded, so its confidence is reliable) and a generate-method twin (the
# normalised common name). resolve_vendor combines them into the computed final
# ``vendor_name`` -- that final is the critical field, the written value, and what
# the vendor requirement passes on when either raw field clears the threshold.
VENDOR_EXTRACT = "vendor_name_extract"
VENDOR_GENERATE = "vendor_name_generate"
VENDOR_FINAL = "vendor_name"

# service_address is captured twice as well, but resolved differently: the extract
# field is authoritative and the generate twin is a *validator*. The generate value is
# never written; it can only rescue a below-threshold extract when the two agree.
# See resolve_service_address.
SERVICE_ADDRESS_EXTRACT = "service_address_extract"
SERVICE_ADDRESS_GENERATE = "service_address_generate"
SERVICE_ADDRESS_FINAL = "service_address"

# bill_to_address is captured from the customer / Bill To block and twinned like the
# other fields (extract authoritative, generate validator). It is NOT critical and is
# NOT written to Dynamics (absent from WRITE_FIELDS): it exists only to backfill
# service_address when a document carries no SHIP TO / Service Address block at all and
# addresses the invoice solely to the property manager's Bill To block. gates.evaluate
# promotes it only when service_address is empty and it is not Noble's own head office
# (see is_noble_office_address).
BILL_TO_ADDRESS_EXTRACT = "bill_to_address_extract"
BILL_TO_ADDRESS_GENERATE = "bill_to_address_generate"
BILL_TO_ADDRESS_FINAL = "bill_to_address"

# total_invoice_amount, gst_amount and po_or_job_number are twinned too. The final keeps
# its original name (already the critical + write name); CU now returns the raw twins.
TOTAL_EXTRACT = "total_invoice_amount_extract"
TOTAL_GENERATE = "total_invoice_amount_generate"
TOTAL_FINAL = "total_invoice_amount"

GST_EXTRACT = "gst_amount_extract"
GST_GENERATE = "gst_amount_generate"
GST_FINAL = "gst_amount"

# pst_amount is twinned like gst_amount but is informational only (never critical):
# most invoices are service-only and charge no BC PST. The written value defaults
# to 0 when the twins resolve to nothing or N/A (see build_write_values).
PST_EXTRACT = "pst_amount_extract"
PST_GENERATE = "pst_amount_generate"
PST_FINAL = "pst_amount"

PO_EXTRACT = "po_or_job_number_extract"
PO_GENERATE = "po_or_job_number_generate"
PO_FINAL = "po_or_job_number"

# invoice_number and account_number are twinned identifiers: the extract is the
# value as printed (spaces stripped, dashes/dots kept by the analyzer prompt) and
# the generate twin only validates it. Both are critical for the municipal bucket
# only; commercial bills capture them informationally.
INVOICE_EXTRACT = "invoice_number_extract"
INVOICE_GENERATE = "invoice_number_generate"
INVOICE_FINAL = "invoice_number"

ACCOUNT_EXTRACT = "account_number_extract"
ACCOUNT_GENERATE = "account_number_generate"
ACCOUNT_FINAL = "account_number"

# invoice_date is twinned like the identifiers, but unlike the billing-period dates
# below it IS defaulted: when the twins resolve to nothing usable the write value
# becomes today (build_write_values). A resolved date *after* today is treated as a
# review trigger, not a value to fix (see invoice_date_in_future + gates.evaluate),
# and the generate twin may not carry the field alone (see NO_GENERATE_RESCUE).
INVOICE_DATE_EXTRACT = "invoice_date_extract"
INVOICE_DATE_GENERATE = "invoice_date_generate"
INVOICE_DATE_FINAL = "invoice_date"

# The billing-period fields are twinned and informational only (never critical):
# they feed the tenant utility-sharing calculation downstream on municipal
# utility bills. The dates are normalised but never defaulted -- a wrong period
# date would corrupt the cost sharing, so blank is the correct failure mode (see
# build_write_values). A bill that prints no full start date (a month-only
# period like 'Mar/Apr 2026', or no period at all) derives it deterministically
# from the end date and day count (see derive_billing_period_start).
BILLING_START_EXTRACT = "billing_period_start_date_extract"
BILLING_START_GENERATE = "billing_period_start_date_generate"
BILLING_START_FINAL = "billing_period_start_date"

BILLING_END_EXTRACT = "billing_period_end_date_extract"
BILLING_END_GENERATE = "billing_period_end_date_generate"
BILLING_END_FINAL = "billing_period_end_date"

DAYS_EXTRACT = "number_of_days_extract"
DAYS_GENERATE = "number_of_days_generate"
DAYS_FINAL = "number_of_days"

# Values handed to Power Automate to write to Dynamics. Values only; per-field
# confidence stays in the separate raw-fields block for the review UI and audit.
WRITE_FIELDS: Tuple[str, ...] = (
    "vendor_name",
    "service_address",
    "total_invoice_amount",
    "invoice_date",
    "payment_due_date",
    "invoice_number",
    "po_or_job_number",
    "gst_amount",
    "pst_amount",
    "account_number",
    "bill_type",
    "sub_bill_type",
    "invoice_description",
    "billing_period_start_date",
    "billing_period_end_date",
    "number_of_days",
)
DATE_FIELDS: Tuple[str, ...] = ("invoice_date", "payment_due_date")

MUNICIPAL = "municipal"
COMMERCIAL = "commercial"

# sub_bill_type: informational sub-classification of bill_type. A commercial
# bill derives it from the resolved po_or_job_number (Noble's numbering scheme:
# a format-valid 330... PO is a service job, 110... a repair job); the classified
# label is ignored. A municipal bill resolves the classified label against its
# own (stricter) confidence bar. CU's estimated confidence on classify fields is
# noisy (+-0.3 on identical documents) while the label itself is stable, so a
# generate-method reasoning twin corroborates it: matching labels are accepted
# even below the bar (two independent reads agree). It is never a critical field
# and never gates routing; anything unresolved falls back to "other".
SUB_BILL_TYPE = "sub_bill_type"
SUB_BILL_TYPE_GENERATE = "sub_bill_type_generate"
SUB_BILL_TYPE_THRESHOLD = 0.80
MUNICIPAL_SUB_TYPES: Tuple[str, ...] = ("gas", "electric", "water", "business_license")
SUB_SERVICE = "service"
SUB_REPAIR = "repair"
SUB_OTHER = "other"


# --- bucket resolution -------------------------------------------------------


def resolve_bucket(bill_type_value: Optional[str]) -> str:
    """
    Map the classified ``bill_type`` label to a policy bucket.

    Only an explicit ``municipal`` label yields the municipal (relaxed) bucket.
    Anything else -- ``commercial``, an unexpected value, an empty string, or a
    missing/unreadable label -- resolves to the stricter ``commercial`` bucket.
    This fail-safe lives here as a guard on an absent *type label*; it is not
    field-presence inference (no other field is consulted).
    """
    return MUNICIPAL if (bill_type_value or "").strip().lower() == MUNICIPAL else COMMERCIAL


def critical_fields(bucket: str) -> Tuple[str, ...]:
    """The critical field set for a bucket. The only bucket-dependent rule."""
    if bucket == COMMERCIAL:
        return BASE_CRITICAL + COMMERCIAL_DELTA
    return BASE_CRITICAL + MUNICIPAL_DELTA


# --- per-field format rules --------------------------------------------------

# Exact-format rules applied to a non-empty extracted value: field -> (pattern,
# human hint). A *critical* field whose value fails its pattern is treated as a
# critical-field failure by the B4 gate (routes to review). Empty/missing values
# are handled by the gate's presence check, not here. po_or_job_number is an
# 8-digit, all-numeric identifier that always starts with 110 or 330 (Noble's
# PO/job numbering scheme) -- matched as text, never parsed as a number.
_PO_EXACT = re.compile(r"(?:110|330)\d{5}")
FIELD_FORMATS: Dict[str, Tuple["re.Pattern[str]", str]] = {
    "po_or_job_number": (_PO_EXACT, "exactly 8 digits starting with 110 or 330"),
}


def format_violation_reason(field: str, value: Any) -> Optional[str]:
    """Return a short hint when ``value`` violates ``field``'s format rule, else
    None. A field with no rule, or an empty/None value, is never a violation
    here -- presence and confidence are the gate's responsibility."""
    rule = FIELD_FORMATS.get(field)
    if rule is None or value is None:
        return None
    pattern, hint = rule
    text = str(value).strip()
    if text == "" or pattern.fullmatch(text):
        return None
    return hint


# --- sub_bill_type resolution --------------------------------------------------


def resolve_sub_bill_type(
    bucket: str,
    value: Optional[str],
    confidence: Optional[float],
    po_value: Any,
    generate_value: Optional[str] = None,
) -> str:
    """
    Resolve the written ``sub_bill_type`` for a bucket.

    Informational only -- never gates routing; ``other`` is the fallback, not a
    review trigger.

    A commercial bill ignores the classified label entirely: the sub-type is
    derived from the resolved po_or_job_number (Noble's numbering scheme encodes
    it). A format-valid PO starting with 33 is a ``service`` job, one starting
    with 11 a ``repair`` job. A missing or format-violating PO is guaranteed
    wrong, so it never drives the sub-type -- the bill resolves to ``other``.

    A municipal bill resolves the classified label: it is trusted when it clears
    SUB_BILL_TYPE_THRESHOLD (stricter than the critical-field THRESHOLD) OR when
    the generate reasoning twin returns the same label -- agreement between two
    independent reads corroborates a below-bar label, exactly like the twin
    agreement boost, because the estimated confidence is noisy while the label is
    stable. The generate twin only validates; it never supplies the label itself.
    Only the municipal sub-types (gas/electric/water/business_license) are
    accepted; everything else (unconfirmed below-bar labels, unknown or
    cross-bucket labels) resolves to ``other``.
    """
    if bucket != MUNICIPAL:
        po_text = "" if po_value is None else str(po_value).strip()
        if po_text == "" or format_violation_reason(PO_FINAL, po_text) is not None:
            return SUB_OTHER
        if po_text.startswith("33"):
            return SUB_SERVICE
        if po_text.startswith("11"):
            return SUB_REPAIR
        return SUB_OTHER
    label = (value or "").strip().lower()
    confident = confidence is not None and confidence >= SUB_BILL_TYPE_THRESHOLD
    agree = label != "" and label == (generate_value or "").strip().lower()
    if not (confident or agree):
        return SUB_OTHER
    return label if label in MUNICIPAL_SUB_TYPES else SUB_OTHER


# Contiguous 8-digit 110/330 run not embedded in a longer digit run, so a
# 9-digit artifact like 330001022 never yields a false 33000102.
_PO_CONTIGUOUS = re.compile(r"(?<!\d)(?:110|330)\d{5}(?!\d)")
# Maximal run of digits separated by single spaces/tabs (OCR sometimes spaces
# digits out, e.g. "1102 4580"). Matched as whole runs so adjacent numbers
# ("11024580 5.00") are judged together and rejected, never merged into a hit.
_PO_SPACED_RUN = re.compile(r"\d(?:[ \t]?\d)*")


def find_po_candidates(text: Optional[str]) -> List[str]:
    """Distinct PO/job-number candidates found in free document text, in order
    of first appearance. A candidate is an 8-digit 110/330 number, either
    contiguous or with spaces/tabs between the digits (normalised to contiguous
    digits). Used to rescue a PO the analyzer missed from the OCR markdown."""
    if not text:
        return []
    seen: set = set()
    out: List[str] = []
    for m in _PO_CONTIGUOUS.finditer(text):
        v = m.group()
        if v not in seen:
            seen.add(v)
            out.append(v)
    for m in _PO_SPACED_RUN.finditer(text):
        run = m.group()
        if " " in run or "\t" in run:
            digits = re.sub(r"\s", "", run)
            if _PO_EXACT.fullmatch(digits) and digits not in seen:
                seen.add(digits)
                out.append(digits)
    return out


def invoice_number_default(file_name: Any) -> Optional[str]:
    """The fallback invoice number for a municipal bill whose twins produced
    nothing: the SharePoint filename without its extension, punctuation and
    casing kept as uploaded. Returns None when no usable filename was provided,
    so the caller leaves the field failing (review) exactly as before."""
    if file_name is None:
        return None
    text = str(file_name).strip()
    if not text:
        return None
    return PurePath(text).stem or None


# --- date handling -----------------------------------------------------------


def _normalize_date(value: Any) -> Optional[str]:
    """
    Normalise a Content Understanding date value to YYYY-MM-DD, independent of
    whatever wire format CU returns (we do not assume it). Returns None when the
    value is absent or cannot be parsed unambiguously.

    Ambiguous all-numeric formats (e.g. 03/04/2026, which could be Mar 4 or
    Apr 3) are deliberately treated as unparseable -> the date defaults. A wrong
    guess on a financial date is worse than substituting today's date, and CU's
    ``date`` type is expected to deliver an ISO value anyway.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime(DATE_FORMAT)
    if isinstance(value, date):
        return value.strftime(DATE_FORMAT)

    text = str(value).strip()
    if not text:
        return None

    # ISO and ISO-like (handles 2026-06-29, 2026-06-29T00:00:00, ...Z).
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(DATE_FORMAT)
    except ValueError:
        pass

    # Unambiguous explicit formats only (year-first numeric, or month-name).
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime(DATE_FORMAT)
        except ValueError:
            continue

    return None


def _now_pacific(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(BUSINESS_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BUSINESS_TZ)
    return now.astimezone(BUSINESS_TZ)


def invoice_date_in_future(value: Any, now: Optional[datetime] = None) -> bool:
    """
    True when an invoice date falls after today's business-timezone calendar day.

    A future issue date is either a misread or a document that should not be paid
    yet, so the caller routes the run to review (gates.evaluate). Today itself is
    not in the future, and an absent or unparseable value never is -- that case is
    already handled by defaulting.
    """
    normalized = _normalize_date(value)
    if normalized is None:
        return False
    return datetime.strptime(normalized, DATE_FORMAT).date() > _now_pacific(now).date()


# Three-letter month prefixes, indexed by month number - 1. Used to recognise a date
# printed in month-name form in the OCR text (see date_corroborated_in_text).
_MONTH_PREFIXES = (
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
)


def date_corroborated_in_text(value: Any, text: str) -> Optional[str]:
    """
    The normalised YYYY-MM-DD when ``value`` is printed in ``text`` as an unambiguous
    month-name or ISO date ("Jun 17, 2026", "17 June 2026", "2026-06-17"), else None.

    This grounds a date the generate twin produced on its own: the twin answers even when
    the document states no date, so its value is only trustworthy when the same calendar
    day is actually printed. Slashed forms (1/13/26) deliberately never corroborate --
    they are ambiguous (see _normalize_date), and the observed false positive, a page
    print timestamp, is always printed that way.

    Whitespace in the haystack is collapsed first, so a date broken across an OCR line
    ("May\\n26, 2026") still matches.
    """
    normalized = _normalize_date(value)
    if normalized is None or not text:
        return None
    flat = re.sub(r"\s+", " ", text)
    year, month, day = normalized.split("-")
    month_name = _MONTH_PREFIXES[int(month) - 1]
    patterns = (
        rf"\b{month_name}[a-z]*\.?\s+0?{int(day)},?\s+{year}\b",
        rf"\b0?{int(day)}\s+{month_name}[a-z]*\.?\s+{year}\b",
        rf"\b{normalized}\b",
    )
    if any(re.search(p, flat, re.IGNORECASE) for p in patterns):
        return normalized
    return None


# --- derived value -----------------------------------------------------------


def _amount_excluding_gst(total: Any, gst: Any) -> Optional[float]:
    """
    total - gst, when both are present. In a multi-tax province this equals
    (subtotal + PST), i.e. literally "amount excluding GST" -- not a pre-tax
    subtotal. Returns None when either input is missing or non-numeric (e.g. a
    municipal bill with no GST line).
    """
    if total is None or gst is None:
        return None
    try:
        return round(float(total) - float(gst), 2)
    except (TypeError, ValueError):
        return None


def derive_billing_period_start(
    end_value: Any,
    end_conf: Optional[float],
    days_value: Any,
    days_conf: Optional[float],
) -> Optional[Tuple[str, float]]:
    """
    Derived billing-period start for a bill that prints no full start date:
    start = end - (number_of_days - 1), the period counted inclusive of both
    endpoints (an Abbotsford 'Mar/Apr 2026' bill read on Apr 30 with 61 days
    starts Mar 1). Returns (YYYY-MM-DD, confidence) with the confidence the
    weaker of the two inputs, or None when either input is missing or
    unparseable -- the caller then leaves the field blank (never defaulted).
    """
    end_norm = _normalize_date(end_value)
    days = _days_int(days_value)
    if end_norm is None or days is None:
        return None
    start = datetime.strptime(end_norm, DATE_FORMAT) - timedelta(days=days - 1)
    return start.strftime(DATE_FORMAT), min(end_conf or 0.0, days_conf or 0.0)


# --- twin field resolution (extract + generate) ------------------------------

# Corporate suffixes dropped before comparing the two vendor spellings, so
# "FortisBC Energy Inc." and "FortisBC" are recognised as the same vendor.
_VENDOR_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "ltd", "limited", "llc", "llp", "corp", "corporation", "co", "company"}
)


def _normalize_vendor(value: Any) -> str:
    """Lowercase, drop punctuation, collapse whitespace, and strip trailing legal
    suffixes so two spellings of the same vendor compare equal."""
    if value is None:
        return ""
    tokens = [t for t in re.sub(r"[^\w\s]", " ", str(value).lower()).split() if t]
    while tokens and tokens[-1] in _VENDOR_LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _vendor_values_consistent(a: Any, b: Any) -> bool:
    """True when both name the same vendor: equal after normalisation, one contained in
    the other (a short common name vs a longer legal name), or one's words a subset of
    the other's. The subset form catches a shortened personal name -- 'Simon Kan' vs
    'Simon Sik Fai Kan' -- where the dropped words sit in the middle, so containment
    never sees it."""
    na, nb = _normalize_vendor(a), _normalize_vendor(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return ta <= tb or tb <= ta


def _prefer_vendor_generate(
    e_val: Any, e_conf: Optional[float], g_val: Any, g_conf: Optional[float]
) -> bool:
    """Which spelling to write when the two vendor twins agree. The generate twin exists
    to normalise the name, so its spelling wins whenever both twins name the vendor
    identically after normalisation (they differ only in casing or a legal suffix). When
    the spellings genuinely differ -- a shortened personal name, a dropped descriptor --
    write whichever twin the model was more confident in; a tie keeps the normalised
    generate name."""
    if _normalize_vendor(e_val) == _normalize_vendor(g_val):
        return True
    return (g_conf or 0.0) >= (e_conf or 0.0)


def _field_passes(value: Any, confidence: Optional[float], threshold: float) -> bool:
    """A non-empty value whose confidence clears the threshold. Field-agnostic;
    shared by the vendor and service-address resolvers."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return False
    return confidence is not None and confidence >= threshold


def resolve_twin(
    parsed: Dict[str, Tuple[Any, Optional[float]]],
    extract_key: str,
    generate_key: str,
    threshold: float,
    agree_fn: "Callable[[Any, Any], bool]",
    prefer_generate_on_agree: "Optional[Callable[[Any, Optional[float], Any, Optional[float]], bool]]" = None,
    allow_generate_rescue: bool = True,
) -> Tuple[Any, float, bool, Optional[str], str]:
    """
    Generic extract + generate twin resolution shared by every twin-resolved critical
    field.

    Passes when: the extract clears ``threshold`` on its own; OR the two values agree
    (per ``agree_fn``) -- the agreement boost, corroboration even when each is individually
    below threshold; OR the extract produced *nothing* and a confident generate
    (>= threshold) fills it in. A generate that merely *disagrees* with a present extract
    never passes -- the extract stays authoritative whenever it found a value.

    ``prefer_generate_on_agree`` selects the value preference when the two agree: a
    predicate ``(e_val, e_conf, g_val, g_conf) -> bool`` returning True writes the generate
    value (vendor-name normalisation, see _prefer_vendor_generate). None -- every field but
    vendor -- keeps the literal extract value (addresses, amounts, PO, identifiers: the twin
    is a validator, not a value source).

    ``allow_generate_rescue`` False drops the third pass condition, so an absent extract can
    never be filled in by the generate twin alone (see NO_GENERATE_RESCUE).

    Returns ``(value, effective_confidence, passed, note, source)``. ``source`` is
    ``"generate"`` (generate carried it: both cleared and agree, or a generate rescue of an
    absent extract), ``"agreement"`` (corroboration carried it), ``"extract"``, or
    ``"none"``. ``note`` is a short advisory when both present values disagree.
    """
    e_val, e_conf = parsed.get(extract_key, (None, None))
    g_val, g_conf = parsed.get(generate_key, (None, None))

    e_pass = _field_passes(e_val, e_conf, threshold)
    g_pass = _field_passes(g_val, g_conf, threshold)

    e_present = e_val is not None and str(e_val).strip() != ""
    g_present = g_val is not None and str(g_val).strip() != ""
    agree = agree_fn(e_val, g_val)

    # A confident generate rescues an *absent* extract (nothing to disagree with); a
    # generate that disagrees with a *present* extract never overrides it.
    generate_rescue = allow_generate_rescue and g_pass and not e_present
    passed = e_pass or agree or generate_rescue

    if (prefer_generate_on_agree is not None and agree and g_present
            and prefer_generate_on_agree(e_val, e_conf, g_val, g_conf)):
        value, source = g_val, ("generate" if (e_pass and g_pass) else "agreement")
    elif e_pass:
        value, source = e_val, "extract"
    elif agree:
        value, source = (e_val if e_present else g_val), "agreement"
    elif generate_rescue:
        value, source = g_val, "generate"
    elif e_present:
        value, source = e_val, "extract"
    elif g_present:
        value, source = g_val, "generate"
    else:
        value, source = None, "none"

    if source == "agreement":
        effective_conf = max(e_conf or 0.0, g_conf or 0.0)
    elif source == "generate":
        effective_conf = g_conf or 0.0
    else:
        effective_conf = e_conf or 0.0

    note = None
    if e_present and g_present and not agree:
        note = f"{extract_key}/{generate_key} disagree: {e_val!r} vs {g_val!r}"

    return value, effective_conf, passed, note, source


# --- service address resolution (extract authoritative + generate validator) -


def _normalize_address_tokens(value: Any) -> set:
    """Lowercased, punctuation/whitespace-split token set for address comparison."""
    if value is None:
        return set()
    return {t for t in re.sub(r"[^\w\s]", " ", str(value).lower()).split() if t}


def _address_tokens_agree(a: Any, b: Any, min_overlap: float = 0.70) -> bool:
    """
    True when two addresses name the same place: the share of common tokens over the
    *smaller* token set is at least ``min_overlap``. The min-set denominator makes a
    subset (e.g. the generate value) matching part of a superset (the extract value,
    which may include a company-name line) still agree, and is robust to word order
    and unit formatting ("#113 - 8531 Alexandra Rd" vs "8531 Alexandra Rd Unit 113").
    """
    ta, tb = _normalize_address_tokens(a), _normalize_address_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= min_overlap


# Noble's own head/billing office(s) -- the generic paying-party address(es) that name no
# serviced property. When a document has no service address of its own and is addressed only
# to a Bill To block, that block is promoted to service_address (gates.evaluate) UNLESS it is
# one of these. A stable business fact, not a secret. Extend the tuple if Noble bills from
# more than one office. Matched by normalized token overlap, so '#'/'Unit', comma, and
# postal-code spacing differences do not matter -- the street + city tokens carry the match.
NOBLE_OFFICE_ADDRESSES: Tuple[str, ...] = ("155-13988 Maycrest Way, Richmond BC  V6V3C3",)


def is_noble_office_address(value: Any) -> bool:
    """True when an address is one of Noble's own head/billing offices (the paying-party
    address), which must never become the service address."""
    return any(_address_tokens_agree(value, office) for office in NOBLE_OFFICE_ADDRESSES)


def _amounts_agree(a: Any, b: Any) -> bool:
    """True when two amounts are the same money value (equal to the cent). False when
    either is missing or non-numeric."""
    try:
        return abs(round(float(a), 2) - round(float(b), 2)) < 0.005
    except (TypeError, ValueError):
        return False


def _po_values_agree(a: Any, b: Any) -> bool:
    """True when the two PO/job numbers carry the same non-empty digit sequence. The
    8-digit format itself is enforced separately on the resolved value (FIELD_FORMATS)."""
    da = re.sub(r"\D", "", str(a)) if a is not None else ""
    db = re.sub(r"\D", "", str(b)) if b is not None else ""
    return da != "" and da == db


def _identifier_values_agree(a: Any, b: Any) -> bool:
    """True when two identifiers (invoice/account numbers) carry the same non-empty
    letter+digit sequence, case-insensitively. Punctuation/spacing is display
    formatting and is ignored for agreement; the written value keeps the extract's
    printed punctuation. Neither field has a format rule (unlike po_or_job_number)."""
    na = re.sub(r"[^0-9a-z]", "", str(a).lower()) if a is not None else ""
    nb = re.sub(r"[^0-9a-z]", "", str(b).lower()) if b is not None else ""
    return na != "" and na == nb


def _dates_agree(a: Any, b: Any) -> bool:
    """True when two dates name the same calendar day after normalisation. False
    when either is missing or unparseable (an unparseable date corroborates nothing)."""
    na, nb = _normalize_date(a), _normalize_date(b)
    return na is not None and na == nb


def _days_int(value: Any) -> Optional[int]:
    """A positive day count parsed from a CU integer/number or text such as
    '61 days'. None when absent, unparseable, or not positive."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        days = int(value)
        return days if days > 0 else None
    m = re.search(r"\d+", str(value))
    if m is None:
        return None
    days = int(m.group())
    return days if days > 0 else None


def _days_agree(a: Any, b: Any) -> bool:
    """True when both day counts parse to the same positive integer."""
    da, db = _days_int(a), _days_int(b)
    return da is not None and da == db


def resolve_vendor(
    parsed: Dict[str, Tuple[Any, Optional[float]]],
    threshold: float = THRESHOLD,
) -> Tuple[Any, float, bool, Optional[str], str]:
    """vendor_name twin: on agreement writes the clean generate name when the two are the
    same name, else the more confident spelling (see _prefer_vendor_generate)."""
    return resolve_twin(
        parsed, VENDOR_EXTRACT, VENDOR_GENERATE, threshold,
        _vendor_values_consistent, prefer_generate_on_agree=_prefer_vendor_generate,
    )


def resolve_service_address(
    parsed: Dict[str, Tuple[Any, Optional[float]]],
    threshold: float = THRESHOLD,
) -> Tuple[Any, float, bool, Optional[str], str]:
    """service_address twin: the extract is authoritative; the generate twin only validates."""
    return resolve_twin(
        parsed, SERVICE_ADDRESS_EXTRACT, SERVICE_ADDRESS_GENERATE, threshold,
        _address_tokens_agree, prefer_generate_on_agree=None,
    )


# Every twin-resolved field (all critical in some bucket except pst_amount and the
# billing-period fields, which are informational only): final name -> (extract key,
# generate key, agree fn, value preference on agreement). Only vendor supplies a
# preference (the normalised name, or the more confident spelling when the two differ);
# None keeps the literal extract value (the twin only validates).
TWIN_FIELDS: Dict[
    str,
    Tuple[str, str, Callable[[Any, Any], bool],
          Optional[Callable[[Any, Optional[float], Any, Optional[float]], bool]]],
] = {
    VENDOR_FINAL: (VENDOR_EXTRACT, VENDOR_GENERATE, _vendor_values_consistent, _prefer_vendor_generate),
    SERVICE_ADDRESS_FINAL: (SERVICE_ADDRESS_EXTRACT, SERVICE_ADDRESS_GENERATE, _address_tokens_agree, None),
    BILL_TO_ADDRESS_FINAL: (BILL_TO_ADDRESS_EXTRACT, BILL_TO_ADDRESS_GENERATE, _address_tokens_agree, None),
    TOTAL_FINAL: (TOTAL_EXTRACT, TOTAL_GENERATE, _amounts_agree, None),
    GST_FINAL: (GST_EXTRACT, GST_GENERATE, _amounts_agree, None),
    PST_FINAL: (PST_EXTRACT, PST_GENERATE, _amounts_agree, None),
    PO_FINAL: (PO_EXTRACT, PO_GENERATE, _po_values_agree, None),
    INVOICE_FINAL: (INVOICE_EXTRACT, INVOICE_GENERATE, _identifier_values_agree, None),
    ACCOUNT_FINAL: (ACCOUNT_EXTRACT, ACCOUNT_GENERATE, _identifier_values_agree, None),
    INVOICE_DATE_FINAL: (INVOICE_DATE_EXTRACT, INVOICE_DATE_GENERATE, _dates_agree, None),
    BILLING_START_FINAL: (BILLING_START_EXTRACT, BILLING_START_GENERATE, _dates_agree, None),
    BILLING_END_FINAL: (BILLING_END_EXTRACT, BILLING_END_GENERATE, _dates_agree, None),
    DAYS_FINAL: (DAYS_EXTRACT, DAYS_GENERATE, _days_agree, None),
}


# Fields a confident generate twin may NOT carry on its own. invoice_date is the one:
# on a bill that prints no issue date at all (a licence renewal notice showing only a
# due date), the reasoning twin has been observed answering with the page-footer print
# timestamp -- once at 0.82, above the bar -- which would auto-write a plausible-looking
# wrong date with no review. With no extract span backing it there is nothing to
# corroborate the value, and this field has a safe deterministic fallback (today), so an
# ungrounded generate value is refused and the date defaults instead.
NO_GENERATE_RESCUE: frozenset = frozenset({INVOICE_DATE_FINAL})


def resolve_field(
    final_name: str,
    parsed: Dict[str, Tuple[Any, Optional[float]]],
    threshold: float = THRESHOLD,
) -> Tuple[Any, float, bool, Optional[str], str]:
    """Resolve one twin-resolved critical field by its final name."""
    extract_key, generate_key, agree_fn, prefer = TWIN_FIELDS[final_name]
    return resolve_twin(
        parsed, extract_key, generate_key, threshold, agree_fn, prefer,
        allow_generate_rescue=final_name not in NO_GENERATE_RESCUE,
    )


# --- write-values builder ----------------------------------------------------


def build_write_values(
    parsed: Dict[str, Tuple[Any, Optional[float]]],
    threshold: float = THRESHOLD,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Build the clean, ready-to-write value set for Dynamics from the parsed CU
    fields, applying date defaults and computing the derived amount.

    ``parsed`` maps ``field -> (value, confidence)``.

    Returns ``(write_values, defaulted_fields)`` where:
        * date fields are normalised to YYYY-MM-DD; if empty/unparseable or not
          reliable they are replaced (invoice_date -> today PST,
          payment_due_date -> today + 30 PST) and the field name is recorded in
          ``defaulted_fields`` for the ledger. "Reliable" is the twin resolution
          for invoice_date (so two agreeing sub-threshold twins keep the printed
          date) and confidence >= threshold for payment_due_date;
        * non-date fields pass through unchanged (values only);
        * ``pst_amount`` defaults to 0 when the twins resolve to nothing, an
          empty string, or N/A (no PST charged -- the common, service-only case);
        * the billing-period dates are normalised but NEVER defaulted (blank
          when absent/unparseable) and ``number_of_days`` is a positive int or
          blank -- informational fields for the tenant utility-sharing math;
        * ``amount_excluding_gst`` is the derived total - gst (or None).

    This is identical for both buckets -- defaulting is not bucket-dependent.
    """
    now_pst = _now_pacific(now)
    default_for = {
        "invoice_date": now_pst.strftime(DATE_FORMAT),
        "payment_due_date": (now_pst + timedelta(days=DUE_DATE_DEFAULT_DAYS)).strftime(DATE_FORMAT),
    }

    write: Dict[str, Any] = {}
    defaulted: List[str] = []

    # The twin-resolved finals are computed (not straight passthroughs), so Dynamics
    # receives the resolved value for each; CU no longer returns these names directly.
    # invoice_date is both twin-resolved AND defaultable, so the resolution has to feed
    # the date branch below -- hence one pass, not a second loop overwriting the first.
    for name in WRITE_FIELDS:
        if name in TWIN_FIELDS:
            value, confidence, reliable = resolve_field(name, parsed, threshold)[:3]
        else:
            value, confidence = parsed.get(name, (None, None))
            reliable = confidence is not None and confidence >= threshold
        if name in DATE_FIELDS:
            normalized = _normalize_date(value)
            if normalized is not None and reliable:
                write[name] = normalized
            else:
                write[name] = default_for[name]
                defaulted.append(name)
        else:
            write[name] = value

    # A line break inside a company name is an OCR artifact, not part of the name, and it
    # flips run to run on the same document ("WASTE MANAGEMENT\nOF CANADA CORPORATION" vs
    # the spaced form). Collapse it so Dynamics gets one stable spelling. Vendor only --
    # service_address is legitimately multi-line.
    vendor = write[VENDOR_FINAL]
    if isinstance(vendor, str):
        write[VENDOR_FINAL] = " ".join(vendor.split())

    # pst_amount is written as 0 when no PST is charged (most invoices are
    # service-only) or the twins resolved to N/A/empty -- Dynamics gets a number.
    pst = write[PST_FINAL]
    if pst is None or (isinstance(pst, str) and pst.strip().lower() in ("", "n/a", "na")):
        write[PST_FINAL] = 0

    # The billing-period fields are informational and never default: the dates
    # are normalised to YYYY-MM-DD and blank when absent or unparseable (a wrong
    # period date would corrupt the tenant utility-sharing calculation, so blank
    # beats a substituted date), the day count a positive integer or blank.
    # gates.evaluate derives a missing start date from the resolved end date and
    # day count afterwards (derive_billing_period_start).
    for name in (BILLING_START_FINAL, BILLING_END_FINAL):
        write[name] = _normalize_date(write[name]) or ""
    days = _days_int(write[DAYS_FINAL])
    write[DAYS_FINAL] = days if days is not None else ""

    # sub_bill_type is derived too: commercial from the resolved PO's prefix,
    # municipal from the classified label (confidence bar / generate-twin
    # agreement). gates.evaluate refreshes it after the OCR PO rescue, which can
    # change the PO the commercial rule depends on.
    sub_value, sub_confidence = parsed.get(SUB_BILL_TYPE, (None, None))
    write[SUB_BILL_TYPE] = resolve_sub_bill_type(
        resolve_bucket(parsed.get("bill_type", (None, None))[0]),
        sub_value, sub_confidence, write[PO_FINAL],
        parsed.get(SUB_BILL_TYPE_GENERATE, (None, None))[0],
    )

    # amount_excluding_gst is derived from the resolved total and gst, not the raw CU
    # fields (which are now the *_extract / *_generate twins).
    write["amount_excluding_gst"] = _amount_excluding_gst(write[TOTAL_FINAL], write[GST_FINAL])

    return write, defaulted
