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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

# --- constants ---------------------------------------------------------------

POLICY_VERSION = "commercial-narrative-v9"

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

# The narrative fields: what the vendor found and did, what they suggest doing next,
# and what they warrant. Lone generate fields -- a free-text summary has no single
# span to extract, so there is no twin to agree with (same shape as
# invoice_description). They are informational only: never critical, never routing,
# never defaulted. Character limits live in the analyzer prompts, not here.
DIAGNOSIS_FINAL = "diagnosis_solution"
DIAGNOSIS_ZH_FINAL = "diagnosis_solution_zh_hant"
RECOMMENDATION_FINAL = "recommendation"
RECOMMENDATION_ZH_FINAL = "recommendation_zh_hant"
WARRANTY_FINAL = "warranty"

# The only bucket-conditioned write rule. A municipal utility bill has no diagnosis,
# no recommended follow-up work and no warranty, so these are forced blank there
# rather than trusting five generate prompts not to invent content on a water bill.
# The prompts say the same thing, but a code gate is deterministic and testable
# offline; the prompt alone is not. See build_write_values.
COMMERCIAL_ONLY_FIELDS: Tuple[str, ...] = (
    DIAGNOSIS_FINAL,
    DIAGNOSIS_ZH_FINAL,
    RECOMMENDATION_FINAL,
    RECOMMENDATION_ZH_FINAL,
    WARRANTY_FINAL,
)

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
    DIAGNOSIS_FINAL,
    DIAGNOSIS_ZH_FINAL,
    RECOMMENDATION_FINAL,
    RECOMMENDATION_ZH_FINAL,
    WARRANTY_FINAL,
)
DATE_FIELDS: Tuple[str, ...] = ("invoice_date", "payment_due_date")

# payment_due_date is a lone extract -- no twin, so no agreement boost to carry a
# sub-threshold read. Named here because gates.evaluate rescues it by corroboration.
PAYMENT_DUE_FINAL = "payment_due_date"

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


# A city bill CU classifies 'water' must actually bill a water service. CU reads
# a fire-protection line (fireline) fee as 'water' because a fireline is a water
# line, so a city bill whose only utility charges are a fireline fee and/or
# street cleaning is labelled 'water' even though those flat levies are not
# water/sewer/stormwater service -- the business-correct sub-type is 'other'.
# This is decided on the OCR text (the content signal the classify label
# misreads), not the noisy classify confidence, and deliberately in code: a
# sub_bill_type *prompt* edit perturbs unrelated fields CU extracts from the same
# document, so the analyzer definition is left untouched.
_WATER_SERVICE_RE = re.compile(
    r"\b(?:waste\s?water|storm\s?water|water|sewer|sewage|drainage)\b", re.IGNORECASE
)


def water_service_indicated(text: Optional[str]) -> bool:
    """True when the document text names a water/sewer/stormwater service -- the
    signal that a 'water' sub_bill_type is genuine. False for a bill that never
    names such a service (for example a fireline + street-cleaning city bill)."""
    return bool(text) and _WATER_SERVICE_RE.search(text) is not None


def demote_non_water_sub_type(sub_bill_type: Any, text: Optional[str]) -> Any:
    """Demote a 'water' sub_bill_type to 'other' when the document names no water
    service (see water_service_indicated). Any other value passes through
    unchanged, so this is a no-op for every non-'water' bill."""
    if str(sub_bill_type or "").strip().lower() == "water" and not water_service_indicated(text):
        return SUB_OTHER
    return sub_bill_type


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


def amount_excluding_gst(total: Any, gst: Any) -> Optional[float]:
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


# --- sectioned-bill GST ------------------------------------------------------

GST_RATE = 0.05
# How far the GST identity below may miss and still corroborate: 2 cents, or 1% of
# the expected GST, whichever is larger. Both bounds are pinned by real documents.
# The floor covers per-section cent rounding (worst measured 0.9 cents on a 66.39
# bill). The ratio exists for FortisBC: its BC clean energy levy is charged but NOT
# GST-taxable, a systematic 0.40%-of-GST miss on every FortisBC bill (228.08/10.82
# residual 0.043). Above that the next cluster starts at 7.0% (a trade invoice
# whose admin fee is printed "incl. 5% GST") and runs to 876% (a bill carrying an
# untaxed security deposit) -- both of which MUST stay outside. The gap between
# 0.4% and 7% is where this tolerance lives; widening it lets a wrong amount
# corroborate.
GST_IDENTITY_TOLERANCE = 0.02
GST_IDENTITY_TOLERANCE_RATIO = 0.01


def gst_consistent_with_total(gst: Any, total: Any, pst: Any = 0) -> bool:
    """
    True when ``gst`` is ~5% of this bill's pre-tax amount (total - gst - pst).

    Asymmetric evidence: a True means the amount fits the bill's arithmetic and is
    almost certainly the document-level GST. A False means nothing on its own --
    any non-taxable charge (a security deposit, a levy, a fee quoted tax-included)
    breaks the identity for the *correct* value too. Only True is ever acted on.
    """
    g, t = _money(gst), _money(total)
    if g is None or t is None:
        return False
    expected = GST_RATE * (t - g - (_money(pst) or 0.0))
    tolerance = max(GST_IDENTITY_TOLERANCE, GST_IDENTITY_TOLERANCE_RATIO * abs(expected))
    return abs(g - expected) <= tolerance


def _money(value: Any) -> Optional[float]:
    """A money value parsed from a CU number or string, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


_GST_LABEL = re.compile(r"(?i)\bG\.?\s?S\.?\s?T\.?\b|\bgoods and services tax\b")
# A charged amount: optional $, optional thousands separators, exactly two decimals,
# and not a percentage. The two-decimal requirement is what keeps a GST registration
# number printed in the same cell out of the results -- "BC GST 866808298RT0007 |
# $295.61" yields 295.61, and "GST Registration # R121454151" yields nothing. The
# %-exclusion drops the rate column on invoices that print "GST | 5.00% | $23.50".
_GST_MONEY = re.compile(r"\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})(?!\d)(?!\s*%)")
_TABLE_ROW = re.compile(r"<tr>.*?</tr>", re.S)

def find_gst_line_amounts(text: Optional[str]) -> List[float]:
    """
    Distinct GST amounts printed in the document text, in order of first appearance.

    CU renders a bill's charge table either as an HTML table or as flat lines, and
    both shapes occur on FortisBC bills, so both are read: a table row is one unit,
    and in flat text a label line plus the line after it is one unit. Within a unit
    the LAST amount is the charge -- 'TAXES ON ELECTRICITY CHARGES * GST 5% on
    $49.72 | $2.49' prints the base the tax was computed on first and the tax
    itself last.

    Distinct by value on purpose (like find_po_candidates): a bill that prints the
    same GST twice -- one section plus a recap of the same amount -- collapses to a
    single amount, and the caller then declines. The cost is that two sections
    charging *identical* GST also collapse, which under-fires rather than writing a
    wrong sum.
    """
    if not text:
        return []
    out: List[float] = []
    seen: set = set()

    def take(unit: str) -> None:
        label = _GST_LABEL.search(unit)
        if label is None:
            return
        # Only amounts printed AFTER the label belong to it. This drops the neighbouring
        # column on a row that ends with the GST cell ("... | 5,912.20 | BC GST ... |
        # $295.61"), and in flat text it stops a label line from claiming the amount
        # printed above it.
        amounts = [
            float(m.group(1).replace(",", ""))
            for m in _GST_MONEY.finditer(unit[label.end():])
        ]
        if amounts and amounts[-1] not in seen:
            seen.add(amounts[-1])
            out.append(amounts[-1])

    spans = [(m.start(), m.end()) for m in _TABLE_ROW.finditer(text)]
    for start, end in spans:
        take(re.sub(r"\s+", " ", text[start:end]))

    lines = text.split("\n")
    offset = 0
    for i, line in enumerate(lines):
        line_start, offset = offset, offset + len(line) + 1
        if any(a <= line_start < b for a, b in spans):
            continue  # already read as part of a table row
        take(line + " | " + (lines[i + 1] if i + 1 < len(lines) else ""))
    return out


def resolve_sectioned_gst(
    amounts: List[float], current: Any, total: Any, pst: Any = 0
) -> Optional[float]:
    """
    The bill-level GST for a utility bill that taxes each charge section separately,
    or None when the rule does not apply.

    A sectioned bill prints a GST line under each section and may or may not print a
    bill-level recap. When a recap IS printed one of the amounts equals the sum of
    the others, and that amount is the answer; when it is not, the sections add up
    to it. Both branches are then checked against the bill's own arithmetic
    (gst_consistent_with_total), so a sum that does not fit the total is never
    written -- text proposes the candidate, arithmetic confirms it, neither is
    trusted alone.

    Declines (returns None) when fewer than two GST amounts are printed, when the
    candidate is what was already resolved, when the candidate fails the identity,
    or when the current value already satisfies it.
    """
    if len(amounts) < 2:
        return None

    candidate = None
    total_printed = sum(amounts)
    for amount in amounts:
        # "The others" by subtraction rather than by filtering the list, so a line that
        # happens to carry the same amount as another is not silently dropped.
        if abs(amount - (total_printed - amount)) < 0.005:
            candidate = amount  # a printed recap covering the section lines
            break
    if candidate is None:
        candidate = round(sum(amounts), 2)

    current_value = _money(current)
    if current_value is not None and abs(candidate - current_value) < 0.005:
        return None
    if not gst_consistent_with_total(candidate, total, pst):
        return None
    if gst_consistent_with_total(current_value, total, pst):
        return None
    return candidate


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


# A "does business as" connector joining a legal entity to its trade name, e.g.
# "Graffiti Guys Removal Services / dba Goodbye Graffiti Surrey". Word-boundary anchored
# so it cannot fire inside an ordinary vendor word.
_DBA_CONNECTOR = re.compile(r"(?i)(?:^|(?<=[\s,.;:()\-]))(?:dba|d/b/a|d\.b\.a\.?)(?=$|[\s,.;:()\-])")


def _has_dba_clause(value: Any) -> bool:
    """True when the printed vendor name carries a 'does business as' connector."""
    return isinstance(value, str) and _DBA_CONNECTOR.search(value) is not None


def _prefer_vendor_generate(
    e_val: Any, e_conf: Optional[float], g_val: Any, g_conf: Optional[float]
) -> bool:
    """Which spelling to write when the two vendor twins agree. The generate twin exists
    to normalise the name, so its spelling wins whenever both twins name the vendor
    identically after normalisation (they differ only in casing or a legal suffix). When
    the spellings genuinely differ -- a shortened personal name, a dropped descriptor --
    write whichever twin the model was more confident in; a tie keeps the normalised
    generate name."""
    # An extract carrying a "dba" clause printed both the legal entity and the trade name;
    # the generate twin returns only the short common name, which on such a bill is the
    # trade name alone ("Goodbye Graffiti" for "Graffiti Guys Removal Services dba Goodbye
    # Graffiti Surrey"). The trade name "agrees" by substring containment, so without this
    # the confidence tiebreak below would discard the fuller printed name whenever the
    # generate twin scored higher -- which is the bug: a 0.710 trade name beat the correct
    # 0.662 extract. The printed name wins on these bills regardless of confidence.
    if _has_dba_clause(e_val):
        return False
    if _normalize_vendor(e_val) == _normalize_vendor(g_val):
        return True
    return (g_conf or 0.0) >= (e_conf or 0.0)


# Domain labels that never identify a vendor. A sole proprietor billing from
# bon2k1@hotmail.com corroborates nothing, and "Billing@NobleHomes.ca" printed on a
# vendor's invoice names the CUSTOMER -- corroborating against it would promote Noble
# itself as the vendor. TLDs and hosting labels are here for the same reason.
_NON_VENDOR_DOMAIN_LABELS = frozenset({
    "gmail", "hotmail", "outlook", "yahoo", "live", "icloud", "aol", "msn", "protonmail",
    "shaw", "telus", "rogers", "bell", "sympatico",
    "com", "net", "org", "ca", "gov", "bc", "www", "site", "my", "co", "uk",
    "noblehomes", "nobleassociates", "nobleandassociates",
})

# A URL or e-mail address in the OCR text: https://romaheating.ca/, www.priorityappliance.com,
# dispatch@priorityappliance.com. Anchored on the scheme, the www. prefix, or the @ so a bare
# sentence containing a dot cannot match.
_VENDOR_DOMAIN_RE = re.compile(r"(?i)(?:https?://|www\.|@)([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})")


def vendor_domain_labels(text: str) -> Set[str]:
    """
    Domain labels from every URL and e-mail address printed in ``text``, minus the
    generic ones. "www.priorityappliance.com" and "dispatch@priorityappliance.com" both
    yield {"priorityappliance"}.

    A vendor's own web or e-mail domain is printed on its letterhead and is machine
    readable, unlike a stylized logo -- so it is independent evidence of who issued the
    invoice, from a part of the page no field prompt competes over.
    """
    labels: Set[str] = set()
    for host in _VENDOR_DOMAIN_RE.findall(text or ""):
        for label in host.lower().split("."):
            if len(label) >= 4 and label not in _NON_VENDOR_DOMAIN_LABELS:
                labels.add(label)
    return labels


# How much of the longer string the shorter one must account for before a prefix match
# counts as corroboration. A bare prefix test is far too loose: the label "vancouver"
# (from www.vancouver.ca) prefixes "Vancouver Water Works", which would let a city's
# domain promote the wrong municipal vendor. At 0.6 the domain has to be most of the
# name -- "priorityappliance" is 17 of the 24 characters in "priorityapplianceservice"
# (0.71, corroborates) while "vancouver" is 9 of 19 (0.47, does not).
_VENDOR_DOMAIN_OVERLAP = 0.6


def vendor_corroborated_by_domain(value: Any, labels: Set[str]) -> bool:
    """True when a vendor name matches one of the printed domain labels once both are
    reduced to bare alphanumerics -- "Priority Appliance" and the longer printed
    "PRIORITY appliance service" both match the label "priorityappliance".

    One must be a prefix of the other AND the shorter must account for at least
    _VENDOR_DOMAIN_OVERLAP of the longer, so a short generic label cannot corroborate an
    unrelated longer name. Deliberately conservative: a missed match leaves the existing
    resolution alone, while a false match would overwrite a correctly extracted vendor."""
    key = re.sub(r"[^a-z0-9]", "", str(value).lower()) if value is not None else ""
    if len(key) < 4:
        return False
    for lbl in labels:
        short, long = sorted((key, lbl), key=len)
        if long.startswith(short) and len(short) >= _VENDOR_DOMAIN_OVERLAP * len(long):
            return True
    return False


def vendor_domain_tiebreak(e_val: Any, g_val: Any, text: str) -> Optional[Any]:
    """
    The twin value corroborated by a web/e-mail domain printed in ``text``, when the twins
    name genuinely DIFFERENT vendors and exactly one of them is corroborated. None in every
    other case, meaning "no opinion -- keep the existing resolution".

    The consistency guard is what keeps this safe, and it is not optional: on a municipal
    bill the twins routinely differ only in form -- "The Corporation of Delta" vs "Delta",
    "District of West Vancouver" vs "West Vancouver" -- and the city's own domain (delta.ca,
    westvancouver.ca) always matches the SHORT form. Without the guard this would strip
    "District of" from a correct name. Those two are the same vendor, so there is no tie to
    break; the ordinary twin resolution already handles which spelling to write.

    It fires only where the twins point at different companies, which on the corpus is the
    stylized-logo case: the letterhead name is a graphic, the extract twin takes whatever
    plain text sits nearby, and the domain is the only machine-readable evidence of who
    actually issued the invoice.
    """
    if _vendor_values_consistent(e_val, g_val):
        return None
    labels = vendor_domain_labels(text)
    if not labels:
        return None
    e_ok = vendor_corroborated_by_domain(e_val, labels)
    g_ok = vendor_corroborated_by_domain(g_val, labels)
    if e_ok == g_ok:
        return None
    return e_val if e_ok else g_val


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
# serviced property. gates.evaluate consults this on both paths that can set service_address:
# a Bill To block is promoted only when it is NOT one of these, and a service_address read
# straight off the page is rejected when it IS one. A stable business fact, not a secret.
# Extend the tuple if Noble bills from more than one office. Matched by normalized token
# overlap plus the unit and street numbers (see is_noble_office_address), so '#'/'Unit',
# comma, and postal-code spacing differences do not matter.
NOBLE_OFFICE_ADDRESSES: Tuple[str, ...] = ("155-13988 Maycrest Way, Richmond BC  V6V3C3",)


def _numeric_tokens(tokens: set) -> set:
    """The purely-numeric tokens of an address -- its unit and street numbers. A postal-code
    token ('v6v3c3', '3c3') mixes letters and digits and is excluded, which matters because
    postal codes tokenize inconsistently ('V6V 3C3' vs 'V6V3C3')."""
    return {t for t in tokens if t.isdigit()}


def is_noble_office_address(value: Any) -> bool:
    """True when an address is one of Noble's own head/billing offices (the paying-party
    address), which must never become the service address.

    Token overlap alone is too loose to *reject* an address with: it scores over the smaller
    token set, so 'Richmond, BC' (both tokens shared) and 'Unit 200 - 13988 Maycrest Way'
    (a different unit of the same building) both clear the bar. That is harmless where a match
    only blocks a Bill To promotion, but gates.evaluate also discards a service_address on a
    match. So require the office's own unit and street numbers to be present as well -- they
    are what actually identify the office, and every spelling of it seen in the corpus
    ('155-13988 Maycrest Way', '13988 MAYCREST WAY # 155', '13988 Maycrest Way, Unit 155',
    '155 13988 MAYCREST WAY', 'Unit 155 - 13988 Maycrest Way') carries both.
    """
    value_tokens = _normalize_address_tokens(value)
    return any(
        _address_tokens_agree(value, office)
        and _numeric_tokens(_normalize_address_tokens(office)) <= value_tokens
        for office in NOBLE_OFFICE_ADDRESSES
    )


# --- grounding an address in the printed page --------------------------------

# A markdown table cell / row, and the header labels whose column names the serviced
# property on a municipal licence or permit notice.
_TABLE = re.compile(r"<table>.*?</table>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_LOCATION_HEADER = re.compile(r"(?i)^\s*locations?\s*$")
# A cell holding an address: a street number followed by a street name ending in a street
# type. Deliberately strict -- it is the only thing separating the address cell from the
# fee and total cells sitting in the same column position on a colspan row ('$98').
_STREET = re.compile(
    r"(?i)^\s*#?\s*\d+[\w\-]*\s+.*\b(ave|avenue|blvd|boulevard|cres|crescent|court|ct|"
    r"drive|dr|highway|hwy|lane|ln|place|pl|road|rd|st|street|terrace|way)\b\.?\s*$"
)


def normalize_written_text(value: Any) -> Any:
    """One stable spelling for a written name or address: every run of whitespace becomes a
    single space. Non-strings pass through untouched."""
    return " ".join(value.split()) if isinstance(value, str) else value


def address_corroborated_by_span(value: Any, field_data: Any, text: str) -> bool:
    """
    True when a CU address field's OWN spans point at printed text naming the same place
    as its value.

    Grounds an address the generate twin produced on its own. Stronger evidence than the
    date_corroborated_in_text precedent, which can only ask whether the value appears
    somewhere on the page: a span says *this is where CU read it*, so a value taken from
    the letterhead or invented outright cannot be laundered by a page-wide hit. Agreement
    uses the same token-overlap comparator as the twin resolution, because the span quotes
    the surrounding line ('Job# 11024580 | key stuck: 8631 Alexandra Road, Richmond BC')
    while the value is the cleaned address.
    """
    if not isinstance(field_data, dict) or not text:
        return False
    spans = field_data.get("spans")
    if not isinstance(spans, list) or not spans:
        return False
    quoted = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        offset, length = span.get("offset"), span.get("length")
        if isinstance(offset, int) and isinstance(length, int) and length > 0:
            quoted.append(text[offset:offset + length])
    return bool(quoted) and _address_tokens_agree(value, " ".join(quoted))


def licence_location_address(text: str) -> Optional[str]:
    """
    The serviced property read from the 'Locations' column of a municipal licence or
    permit notice, or None when the document has no such column.

    A City of Vancouver business-licence renewal prints the licensed premises ONLY as a
    table cell under a 'Locations' header -- no SHIP TO, Service Address or Attention
    block anywhere on the page. Both CU twins are steered by a list of address-block
    labels, so neither claims a bare table cell reliably: on business_license.pdf the
    extract twin returns nothing on every replicate and the generate twin abstains
    entirely on 1 run in 10, which leaves a base-critical field empty on a document that
    plainly names the property.

    Same shape as resolve_sectioned_gst: the printed text proposes a candidate and an
    independent check confirms it. Here the check is the address shape (_STREET), which
    is what separates the premises cell from the fee cell that a colspan row leaves in
    the same column position. Declines unless exactly one distinct address-shaped value
    is found, so a multi-site licence goes to a human rather than having one of its sites
    picked arbitrarily.
    """
    if not text:
        return None
    found: List[str] = []
    for table in _TABLE.findall(text):
        rows = [
            [re.sub(r"<[^>]+>", " ", cell).strip() for cell in _CELL.findall(row)]
            for row in _TABLE_ROW.findall(table)
        ]
        if not rows:
            continue
        columns = [i for i, head in enumerate(rows[0]) if _LOCATION_HEADER.match(head)]
        for index in columns:
            for row in rows[1:]:
                if index < len(row) and _STREET.match(row[index]):
                    value = " ".join(row[index].split())
                    if value not in found:
                        found.append(value)
    return found[0] if len(found) == 1 else None


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
        * the narrative fields (``COMMERCIAL_ONLY_FIELDS``) are blanked on the
          municipal bucket and coerced to a string otherwise;
        * ``amount_excluding_gst`` is the derived total - gst (or None).

    Defaulting is not bucket-dependent; the narrative blanking is the single
    bucket-conditioned write rule.
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

    # A line break inside a company name or an address is an OCR artifact, not part of the
    # value, and it flips run to run on the same document ("WASTE MANAGEMENT\nOF CANADA
    # CORPORATION" vs the spaced form; "4338 Pandora St\nBurnaby , BC" 2 runs in 10 against
    # the spaced form the other 8). Collapse it so Dynamics gets one stable spelling and the
    # same property groups together across invoices.
    for name in (VENDOR_FINAL, SERVICE_ADDRESS_FINAL):
        write[name] = normalize_written_text(write[name])

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
    # A day count is only meaningful for a bill that HAS a billing period, so it is kept
    # only when the period end date resolved. On bills printing no period at all the
    # reasoning twin has been observed inventing "1" (0.45-0.98 across four corpus docs)
    # where the extract twin correctly returned nothing -- and that count feeds the tenant
    # utility-sharing math and the billing-start derivation below.
    #
    # Keyed on the period rather than on confidence or on which twin answered, because
    # neither of those separates the two cases: a legitimate count can come from the
    # generate twin alone (bug_260609_0031 reads 19 only there on 2 runs in 3, corroborated
    # by its printed 2026-05-08..2026-05-26 period) and can sit below the bar, while an
    # invented one has been seen at 0.984. "Is there a period?" separates them cleanly.
    # ...OR the count is grounded on the page in its own right: abbotsford_water prints a
    # DAYS column its extract twin reads 8/8 (0.74-0.99) while its period end -- a meter
    # reading date supplied by the generate twin -- intermittently fails to resolve. A
    # printed count must not be discarded because a *different* field had a bad run.
    days = _days_int(write[DAYS_FINAL])
    days_grounded = resolve_field(DAYS_FINAL, parsed, threshold)[4] in ("extract", "agreement")
    write[DAYS_FINAL] = (
        days if (days is not None and (write[BILLING_END_FINAL] or days_grounded)) else ""
    )

    bucket = resolve_bucket(parsed.get("bill_type", (None, None))[0])

    # sub_bill_type is derived too: commercial from the resolved PO's prefix,
    # municipal from the classified label (confidence bar / generate-twin
    # agreement). gates.evaluate refreshes it after the OCR PO rescue, which can
    # change the PO the commercial rule depends on.
    sub_value, sub_confidence = parsed.get(SUB_BILL_TYPE, (None, None))
    write[SUB_BILL_TYPE] = resolve_sub_bill_type(
        bucket, sub_value, sub_confidence, write[PO_FINAL],
        parsed.get(SUB_BILL_TYPE_GENERATE, (None, None))[0],
    )

    # The narrative fields describe service/supply work, which a municipal utility
    # bill does not report -- a water bill has nothing diagnosed, nothing recommended
    # and nothing warranted. Blanked for that bucket rather than relying on five
    # generate prompts to decline: a generate field answers even when the content is
    # absent (the number_of_days "1" case above is the same failure), and here there
    # is no twin and no confidence bar to catch it. Always a string, so the downstream
    # Dataverse write never sees null for a text column.
    for name in COMMERCIAL_ONLY_FIELDS:
        value = write.get(name)
        write[name] = "" if (bucket == MUNICIPAL or not isinstance(value, str)) else value

    # amount_excluding_gst is derived from the resolved total and gst, not the raw CU
    # fields (which are now the *_extract / *_generate twins).
    write["amount_excluding_gst"] = amount_excluding_gst(write[TOTAL_FINAL], write[GST_FINAL])

    return write, defaulted
