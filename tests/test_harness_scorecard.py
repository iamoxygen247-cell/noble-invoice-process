"""
test_harness_scorecard.py — the harness scorecard must agree with the Function.

Regression guard for the bucket-aware critical-field policy: the per-field .gate
cells that verify_fn.py reconstructs from a decision JSON must flag exactly the
fields the Function's own B4 gate flagged (reviewReasons), for both policy
buckets. Offline: gates.evaluate supplies the decision JSON, no HTTP.

(verify_fn.py absorbed the former local_test.py, so there is one harness now;
the HARNESSES parametrization remains in case a second harness ever returns.)

Run under pytest (the project standard), from the repo root:
    .\\.venv\\Scripts\\python.exe -m pytest
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# The harnesses live in scripts/, the policy modules in functionapp/. Put both on
# sys.path before importing so this suite resolves the same modules the harnesses
# and the Function run.
_REPO = pathlib.Path(__file__).resolve().parent.parent
for _cand in (_REPO / "scripts", _REPO / "functionapp"):
    if str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

import field_policy  # noqa: E402  -- imported after the sys.path bootstrap above
import gates  # noqa: E402
import test as verify_fn  # noqa: E402  -- scripts/verify_fn.py was renamed to scripts/test.py

THRESHOLD = field_policy.THRESHOLD  # 0.73

HARNESSES = [
    pytest.param(verify_fn, id="verify_fn"),
]


# --- CU field / result builders (same shapes as test_field_policy_gates.py) ---


def fstr(value, conf):
    return {"valueString": value, "confidence": conf}


def fnum(value, conf):
    return {"valueNumber": value, "confidence": conf}


def fdate(value, conf):
    return {"valueDate": value, "confidence": conf}


def cu_result(fields, category="general_invoice", analyzer="generalinvoice"):
    """contents[0] = router result (segment category); contents[1] = child fields."""
    return {
        "contents": [
            {"segments": [{"category": category}]},
            {"analyzerId": analyzer, "category": category, "fields": fields},
        ]
    }


def commercial_fields(**overrides):
    base = {
        "vendor_name_extract": fstr("Bob's Plumbing Ltd.", 0.97),
        "service_address_extract": fstr("123 Main St, Vancouver BC", 0.95),
        "total_invoice_amount_extract": fnum(105.0, 0.96),
        "po_or_job_number_extract": fstr("11024580", 0.91),
        "gst_amount_extract": fnum(5.0, 0.93),
        "invoice_date_extract": fdate("2026-05-01", 0.95),
        "invoice_number_extract": fstr("INV-2201", 0.92),
        "bill_type": fstr("commercial", 0.9),
        "sub_bill_type": fstr("repair", 0.9),
        "sub_bill_type_generate": fstr("repair", 0.85),
        "is_handwritten": fstr("no", 0.97),
        "invoice_description": fstr("Electrical repair work.", 0.8),
    }
    base.update(overrides)
    return base


def municipal_fields(**overrides):
    base = {
        "vendor_name_extract": fstr("City of Vancouver", 0.98),
        "service_address_extract": fstr("456 Oak Ave, Vancouver BC", 0.95),
        "total_invoice_amount_extract": fnum(220.0, 0.96),
        # municipal bills usually carry no PO and no GST, but must carry the
        # biller's account number and an invoice/licence number (municipal delta)
        "account_number_extract": fstr("123456789012", 0.95),
        "invoice_number_extract": fstr("BL-123456", 0.93),
        "invoice_date_extract": fdate("2026-05-10", 0.95),
        "bill_type": fstr("municipal", 0.9),
        "sub_bill_type": fstr("business_license", 0.9),
        "sub_bill_type_generate": fstr("business_license", 0.85),
        "is_handwritten": fstr("no", 0.97),
        "invoice_description": fstr("Annual business license renewal.", 0.8),
    }
    base.update(overrides)
    return base


def decision_for(fields):
    return gates.evaluate(cu_result(fields), THRESHOLD)


def scorecard_rows(harness, decision):
    pairs, _run_id = harness.build_scorecard_pairs(decision, "synthetic", "file", THRESHOLD)
    return pairs, dict(pairs)


def review_gate_fields(pairs):
    """Field stems whose .gate cell shows REVIEW — the scorecard's claim of what gated."""
    return {
        label[: -len(".gate")]
        for label, value in pairs
        if label.endswith(".gate") and value.upper().startswith("REVIEW")
    }


# --- municipal bucket: the delta fields must never gate --------------------------


@pytest.mark.parametrize("harness", HARNESSES)
def test_municipal_happy_path_has_no_review_gates(harness):
    decision = decision_for(municipal_fields())
    assert decision["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert decision["reviewReasons"] == []

    pairs, rows = scorecard_rows(harness, decision)
    assert review_gate_fields(pairs) == set(), (
        "the Function said happy path, so no scorecard gate cell may show REVIEW"
    )
    assert rows["policy_bucket"] == field_policy.MUNICIPAL
    assert rows["critical_fields"] == ", ".join(field_policy.critical_fields(field_policy.MUNICIPAL))
    # The commercial-delta fields are absent on this bill and must read not-critical.
    assert rows["po_or_job_number.gate"] == "not critical for this bill type"
    assert rows["gst_amount.gate"] == "not critical for this bill type"
    # The municipal-delta fields are critical here and both clear the bar.
    assert rows["account_number.gate"] == "pass"
    assert rows["invoice_number.gate"] == "pass"


@pytest.mark.parametrize("harness", HARNESSES)
def test_municipal_review_bill_gates_match_review_reasons(harness):
    # The reported bill shape: municipal, vendor + address below threshold, no PO/GST.
    decision = decision_for(
        municipal_fields(
            vendor_name_extract=fstr("City of Vancouver", 0.50),
            service_address_extract=fstr("456 Oak Ave, Vancouver BC", 0.50),
        )
    )
    assert decision["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD
    # One reviewer-facing summary naming every failing field; the per-field
    # diagnostics live in advisoryFlags.
    assert decision["reviewReasons"] == ["vendor_name and service_address need attention"]

    pairs, rows = scorecard_rows(harness, decision)
    assert review_gate_fields(pairs) == {"vendor_name", "service_address"}, (
        "the scorecard must flag exactly the fields the Function's B4 gate flagged"
    )
    # The empty delta fields must not resurface as REVIEW on a municipal bill.
    assert rows["po_or_job_number.gate"] == "not critical for this bill type"
    assert rows["gst_amount.gate"] == "not critical for this bill type"


# --- commercial bucket: the pre-fix behavior must be preserved -------------------


@pytest.mark.parametrize("harness", HARNESSES)
def test_commercial_missing_po_still_reviews(harness):
    fields = commercial_fields()
    del fields["po_or_job_number_extract"]
    decision = decision_for(fields)
    assert decision["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD

    pairs, rows = scorecard_rows(harness, decision)
    assert review_gate_fields(pairs) == {"po_or_job_number"}
    assert rows["po_or_job_number.gate"] == "REVIEW: empty value"
    assert rows["policy_bucket"] == field_policy.COMMERCIAL
    assert rows["critical_fields"] == ", ".join(field_policy.critical_fields(field_policy.COMMERCIAL))


@pytest.mark.parametrize("harness", HARNESSES)
def test_municipal_missing_account_reviews(harness):
    fields = municipal_fields()
    del fields["account_number_extract"]
    decision = decision_for(fields)
    assert decision["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD

    pairs, rows = scorecard_rows(harness, decision)
    assert review_gate_fields(pairs) == {"account_number"}
    assert rows["account_number.gate"] == "REVIEW: empty value"
    assert rows["policy_bucket"] == field_policy.MUNICIPAL


@pytest.mark.parametrize("harness", HARNESSES)
def test_commercial_happy_path_has_no_review_gates(harness):
    decision = decision_for(commercial_fields())
    assert decision["routingDecision"] == gates.HAPPY_PATH_CANDIDATE

    pairs, rows = scorecard_rows(harness, decision)
    assert review_gate_fields(pairs) == set()
    assert rows["po_or_job_number.gate"] == "pass"
    assert rows["gst_amount.gate"] == "pass"
    # The municipal-delta fields never gate a commercial bill. An absent one reads
    # not-critical; a present one that resolves still reads pass (the resolution-first
    # rule), which is informative and does not imply review.
    assert rows["account_number.gate"] == "not critical for this bill type"
    assert rows["invoice_number.gate"] == "pass"


# --- bucket resolution fail-safe --------------------------------------------------


@pytest.mark.parametrize("harness", HARNESSES)
def test_active_critical_fields_fail_safe(harness):
    commercial_set = list(field_policy.critical_fields(field_policy.COMMERCIAL))
    municipal_set = list(field_policy.critical_fields(field_policy.MUNICIPAL))

    assert harness.active_critical_fields({"policyBucket": "municipal"}) == municipal_set
    assert harness.active_critical_fields({"policyBucket": "commercial"}) == commercial_set
    # billType is the fallback for older decision JSON without policyBucket.
    assert harness.active_critical_fields({"billType": "municipal"}) == municipal_set
    # Missing or unknown bucket info fail-safes to the stricter commercial set.
    assert harness.active_critical_fields({}) == commercial_set
    assert harness.active_critical_fields({"policyBucket": "something-else"}) == commercial_set


# --- invoice_description length gate ------------------------------------------------


def test_invoice_description_gate_enforces_the_44_character_rule():
    """The gate must measure what the analyzer prompt actually says.

    It enforced "15 words or fewer" until 2026-08-18, long after b20104f/9a056b8 replaced
    that with "under 44 characters", so the scorecard reported on a rule that no longer
    existed. This is the only programmatic check of the 44-character limit anywhere.
    """
    import scorecard

    gate = scorecard.invoice_description_gate
    assert scorecard.INVOICE_DESCRIPTION_MAX_CHARS == 44

    assert gate("Electrical repair work.") == "pass"                       # 23 chars
    assert gate("Annual business license renewal for 2026.") == "pass"     # 40 chars
    assert gate("x" * 43) == "pass"
    assert gate("x" * 44) == "warning: 44 chars >= 44"                     # "under 44"
    assert gate("x" * 51).startswith("warning: 51 chars")

    # A short phrase of many words passes: the retired rule would have failed it.
    many_short_words = " ".join(["a"] * 20)                                 # 20 words, 39 chars
    assert len(many_short_words) < 44
    assert gate(many_short_words) == "pass"

    assert gate("") == "not critical - empty"
    assert gate(None) == "not critical - empty"
