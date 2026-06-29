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

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# --- constants ---------------------------------------------------------------

POLICY_VERSION = "bill-type-v1"

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
# never a base one. The municipal delta is intentionally empty.
BASE_CRITICAL: Tuple[str, ...] = ("vendor_name", "service_address", "total_invoice_amount")
COMMERCIAL_DELTA: Tuple[str, ...] = ("po_or_job_number", "gst_amount")

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
    "bill_type",
    "invoice_description",
)
DATE_FIELDS: Tuple[str, ...] = ("invoice_date", "payment_due_date")

MUNICIPAL = "municipal"
COMMERCIAL = "commercial"


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
    return BASE_CRITICAL


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
        * date fields are normalised to YYYY-MM-DD; if empty/unparseable or
          confidence < threshold they are replaced (invoice_date -> today PST,
          payment_due_date -> today + 30 PST) and the field name is recorded in
          ``defaulted_fields`` for the ledger;
        * non-date fields pass through unchanged (values only);
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

    for name in WRITE_FIELDS:
        value, confidence = parsed.get(name, (None, None))
        if name in DATE_FIELDS:
            normalized = _normalize_date(value)
            reliable = normalized is not None and confidence is not None and confidence >= threshold
            if reliable:
                write[name] = normalized
            else:
                write[name] = default_for[name]
                defaulted.append(name)
        else:
            write[name] = value

    total = parsed.get("total_invoice_amount", (None, None))[0]
    gst = parsed.get("gst_amount", (None, None))[0]
    write["amount_excluding_gst"] = _amount_excluding_gst(total, gst)

    return write, defaulted
