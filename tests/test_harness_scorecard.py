"""
test_harness_scorecard.py — the harness scorecard must agree with the Function.

Regression guard for the bucket-aware critical-field policy: the per-field .gate
cells that local_test.py / verify_fn.py reconstruct from a decision JSON must
flag exactly the fields the Function's own B4 gate flagged (reviewReasons), for
both policy buckets. Offline: gates.evaluate supplies the decision JSON, no HTTP.

Both harnesses carry a deliberate copy of the same reconstruction logic, so every
test is parametrized over the two modules — a fix landing in only one of them
fails here.

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
import local_test  # noqa: E402
import verify_fn  # noqa: E402

THRESHOLD = field_policy.THRESHOLD  # 0.73

HARNESSES = [
    pytest.param(local_test, id="local_test"),
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
        "po_or_job_number_extract": fstr("00471234", 0.91),
        "gst_amount_extract": fnum(5.0, 0.93),
        "invoice_date": fdate("2026-05-01", 0.95),
        "invoice_number": fstr("INV-2201", 0.92),
        "bill_type": fstr("commercial", 0.9),
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
        # municipal bills usually carry no PO and no GST
        "invoice_date": fdate("2026-05-10", 0.95),
        "bill_type": fstr("municipal", 0.9),
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
    assert rows["critical_fields"] == ", ".join(field_policy.BASE_CRITICAL)
    # The commercial-delta fields are absent on this bill and must read not-critical.
    assert rows["po_or_job_number.gate"] == "not critical for this bill type"
    assert rows["gst_amount.gate"] == "not critical for this bill type"


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
    assert len(decision["reviewReasons"]) == 2
    assert any("vendor_name" in r for r in decision["reviewReasons"])
    assert any("service_address" in r for r in decision["reviewReasons"])

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
def test_commercial_happy_path_has_no_review_gates(harness):
    decision = decision_for(commercial_fields())
    assert decision["routingDecision"] == gates.HAPPY_PATH_CANDIDATE

    pairs, rows = scorecard_rows(harness, decision)
    assert review_gate_fields(pairs) == set()
    assert rows["po_or_job_number.gate"] == "pass"
    assert rows["gst_amount.gate"] == "pass"


# --- bucket resolution fail-safe --------------------------------------------------


@pytest.mark.parametrize("harness", HARNESSES)
def test_active_critical_fields_fail_safe(harness):
    commercial_set = list(field_policy.critical_fields(field_policy.COMMERCIAL))
    base_set = list(field_policy.BASE_CRITICAL)

    assert harness.active_critical_fields({"policyBucket": "municipal"}) == base_set
    assert harness.active_critical_fields({"policyBucket": "commercial"}) == commercial_set
    # billType is the fallback for older decision JSON without policyBucket.
    assert harness.active_critical_fields({"billType": "municipal"}) == base_set
    # Missing or unknown bucket info fail-safes to the stricter commercial set.
    assert harness.active_critical_fields({}) == commercial_set
    assert harness.active_critical_fields({"policyBucket": "something-else"}) == commercial_set
