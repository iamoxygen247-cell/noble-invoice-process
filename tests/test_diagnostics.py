"""
test_diagnostics.py — regression tests for the diagnostics blob sidecar and the
ledger failure/duration stamps.

Guards the diagnostics contract:
  * D1: a happy-path run persists the raw CU result and the decision JSON as two
    blobs under the RowKey prefix, and stamps their paths + AnalyzerId +
    CuDurationMs on the Extracted ledger row.
  * D2: a diagnostics outage is best-effort — the request still returns the full
    decision, the ledger pointers are empty strings, no exception escapes.
  * D3: a CU analyze failure releases the claim — Status=Failed plus
    FailedStage/LastError (truncated) — so the caller's immediate retry reclaims
    and re-processes; a successful re-run clears the stamps. The release is
    conditioned on the failing invocation's own claim etag: if a foreign writer
    touched the row first, nothing is stamped and the row is left as-is.

Offline, same harness as test_function_app_hardening.py: the route is invoked
directly, the REAL ledger.py and diagnostics.save_run code paths execute over
in-memory fakes, and CU is stubbed at the cu_client module boundary.

Run under pytest (the project standard), from the repo root:
    .\\.venv\\Scripts\\python.exe -m pytest
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
for _cand in (_REPO / "scripts", _REPO / "functionapp"):
    if str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from azure.core.exceptions import ResourceExistsError  # noqa: E402

import cu_client  # noqa: E402
import diagnostics  # noqa: E402
import gates  # noqa: E402
import ledger  # noqa: E402

from test_function_app_hardening import (  # noqa: E402
    SOURCE_ID,
    FakeTable,
    as_json,
    commercial_fields,
    cu_result,
    post,
    valid_body,
)


# --- in-memory blob container fake (exercises the real diagnostics.py code) ---


class FakeContainerClient:
    """Mimics the ContainerClient surface diagnostics.py touches."""

    def __init__(self):
        self.blobs = {}
        self.create_calls = 0

    def create_container(self):
        self.create_calls += 1
        if self.create_calls > 1:
            raise ResourceExistsError("container already exists")

    def upload_blob(self, name, data, overwrite=False):
        if name in self.blobs and not overwrite:
            raise ResourceExistsError("blob already exists")
        self.blobs[name] = bytes(data)


@pytest.fixture
def fake_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(ledger, "get_table_client", lambda: table)
    monkeypatch.delenv("FIELD_CONFIDENCE_THRESHOLD", raising=False)
    return table


@pytest.fixture
def fake_container(monkeypatch):
    container = FakeContainerClient()
    monkeypatch.setattr(diagnostics, "get_container_client", lambda: container)
    return container


@pytest.fixture
def cu_stub(monkeypatch):
    calls = []

    def fake_binary(content, file_name=None):
        calls.append(("binary", content, file_name))
        return cu_result(commercial_fields())

    monkeypatch.setattr(cu_client, "analyze_binary", fake_binary)
    return calls


def ledger_row(fake_table):
    pk, rk = ledger.keys_for_source_id(SOURCE_ID)
    return fake_table.rows[(pk, rk)]


# --- D1: happy path persists both blobs and stamps the row ---------------------


def test_happy_path_writes_raw_and_decision_blobs(fake_table, fake_container, cu_stub):
    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    assert payload["status"] == "Extracted"

    _, rk = ledger.keys_for_source_id(SOURCE_ID)
    names = sorted(fake_container.blobs)
    assert len(names) == 2
    decision_name, raw_name = names  # -decision.json sorts before -raw.json
    assert raw_name.startswith(f"{rk}/") and raw_name.endswith("-raw.json")
    assert decision_name.startswith(f"{rk}/") and decision_name.endswith("-decision.json")

    # The raw blob is the full CU result; the decision blob is the gates result.
    raw = json.loads(fake_container.blobs[raw_name])
    assert raw == cu_result(commercial_fields())
    decision = json.loads(fake_container.blobs[decision_name])
    assert decision["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert "writeValues" in decision

    row = ledger_row(fake_table)
    assert row["RawResultBlob"] == raw_name
    assert row["DecisionBlob"] == decision_name
    assert row["AnalyzerId"] == "generalinvoice"
    assert isinstance(row["CuDurationMs"], int) and row["CuDurationMs"] >= 0
    assert row["FailedStage"] == ""
    assert row["LastError"] == ""


def test_save_run_returns_both_paths(fake_container):
    paths = diagnostics.save_run(SOURCE_ID, {"contents": []}, {"routingDecision": "X"})
    assert paths is not None
    raw_path, decision_path = paths
    assert raw_path in fake_container.blobs and decision_path in fake_container.blobs


# --- D2: diagnostics outage never fails the request ----------------------------


def test_blob_outage_is_best_effort(fake_table, cu_stub, monkeypatch):
    def failing_client():
        raise RuntimeError("simulated blob outage")

    monkeypatch.setattr(diagnostics, "get_container_client", failing_client)

    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    assert payload["status"] == "Extracted"
    assert payload["routingDecision"] == gates.HAPPY_PATH_CANDIDATE

    row = ledger_row(fake_table)
    assert row["Status"] == "Extracted"
    assert row["RawResultBlob"] == ""
    assert row["DecisionBlob"] == ""


# --- D3: CU failure stamps the row; a later success clears the stamps ----------


def test_cu_failure_stamps_stage_and_error(fake_table, fake_container, monkeypatch):
    def exploding(content, file_name=None):
        raise RuntimeError("CU exploded: " + "x" * 5000)

    monkeypatch.setattr(cu_client, "analyze_binary", exploding)

    resp = post(valid_body())
    assert resp.status_code == 502
    assert as_json(resp)["status"] == "Failed"

    row = ledger_row(fake_table)
    assert row["Status"] == "Failed", "a CU failure must release the claim"
    assert row["FailedStage"] == "cu_analyze"
    assert row["LastError"].startswith("CU exploded")
    assert len(row["LastError"]) == 1024
    assert fake_container.blobs == {}, "no diagnostics blobs for a failed analyze"


def test_failure_stamp_write_error_still_returns_502(fake_table, fake_container, monkeypatch):
    monkeypatch.setattr(cu_client, "analyze_binary",
                        lambda content, file_name=None: (_ for _ in ()).throw(RuntimeError("CU down")))
    monkeypatch.setattr(ledger, "reclaim",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("table down")))

    resp = post(valid_body())
    assert resp.status_code == 502
    assert "CU down" in as_json(resp)["error"]


def test_failure_release_lost_etag_leaves_row_untouched(fake_table, fake_container, monkeypatch):
    """The release is conditioned on OUR claim's etag: if a foreign writer bumps
    the row between our claim and our failure release, the release must lose
    (412) and stamp nothing — a delayed release must never clobber a newer
    claim (that would let a third request reclaim mid-flight work)."""
    def foreign_write_then_explode(content, file_name=None):
        ledger.upsert(fake_table, SOURCE_ID, Note="foreign")  # bumps the etag
        raise RuntimeError("CU down")

    monkeypatch.setattr(cu_client, "analyze_binary", foreign_write_then_explode)

    resp = post(valid_body())
    assert resp.status_code == 502
    assert as_json(resp)["status"] == "Received"

    row = ledger_row(fake_table)
    assert row["Status"] == "Received", "a lost release must not stamp Failed"
    assert row.get("FailedStage", "") == ""


def test_decision_crash_releases_claim_and_immediate_retry_reprocesses(
        fake_table, fake_container, cu_stub, monkeypatch):
    """An unhandled decision-path bug (gates.evaluate raising) must release the
    claim like a CU failure — Status=Failed, FailedStage=decision, 500 — so the
    caller's automatic retry (which can arrive inside the A1 lease) re-claims
    and re-processes instead of getting a 200 no-op that ends the retry chain."""
    real_evaluate = gates.evaluate
    monkeypatch.setattr(gates, "evaluate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gates bug")))

    resp = post(valid_body())
    assert resp.status_code == 500
    assert as_json(resp)["status"] == "Failed"
    row = ledger_row(fake_table)
    assert row["Status"] == "Failed", "a decision crash must release the claim"
    assert row["FailedStage"] == "decision"
    assert row["LastError"].startswith("gates bug")

    # Immediate retry with the bug gone: reclaims and fully re-processes.
    monkeypatch.setattr(gates, "evaluate", real_evaluate)
    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    assert payload["status"] == "Extracted"
    assert payload["alreadyProcessed"] is False
    row = ledger_row(fake_table)
    assert row["Status"] == "Extracted"
    assert row["FailedStage"] == ""


def test_cu_failure_then_immediate_retry_reprocesses(fake_table, fake_container, cu_stub, monkeypatch):
    # First run: CU fails; the released (Failed) row must be reclaimable at
    # once — the caller's automatic retry fires within seconds, NOT after the
    # A1 lease, so no row-aging here.
    monkeypatch.setattr(cu_client, "analyze_binary",
                        lambda content, file_name=None: (_ for _ in ()).throw(RuntimeError("blip")))
    assert post(valid_body()).status_code == 502
    row = ledger_row(fake_table)
    assert row["Status"] == "Failed"
    assert row["FailedStage"] == "cu_analyze"

    # Immediate retry with CU healthy again: reclaims and fully re-processes.
    monkeypatch.setattr(cu_client, "analyze_binary",
                        lambda content, file_name=None: cu_result(commercial_fields()))

    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    assert payload["status"] == "Extracted"
    assert payload["alreadyProcessed"] is False

    row = ledger_row(fake_table)
    assert row["Status"] == "Extracted"
    assert row["FailedStage"] == ""
    assert row["LastError"] == ""
    assert row["RawResultBlob"].endswith("-raw.json")


# --- B2 rescue through the real HTTP route (integration) -------------------------
# The unit tests cover apply_b2_rescue and _b2_rescue separately; these drive the
# actual route so the wiring between them is exercised too.


def router_only_other():
    """Production shape of an 'other' verdict: CU binds no analyzer to the category,
    so it chains no child and returns one segment with no fields."""
    return {"contents": [{"segments": [{"category": "other"}],
                          "markdown": "CITY OF BURNABY 2026 PROPERTY TAX NOTICE"}]}


def child_only(fields):
    return {"contents": [{"analyzerId": "generalinvoice",
                          "category": "general_invoice", "fields": fields}]}


@pytest.fixture
def cu_stub_other_then(monkeypatch):
    """First analyze call is the router ('other'); the second is the rescue."""
    def make(second):
        calls = []

        def fake_binary(content, file_name=None, analyzer_id=None, timeout=None):
            calls.append(analyzer_id)
            if len(calls) == 1:
                return router_only_other()
            return second() if callable(second) else second

        monkeypatch.setattr(cu_client, "analyze_binary", fake_binary)
        return calls
    return make


def test_b2_rescue_route_recovers_fields(fake_table, fake_container, cu_stub_other_then):
    calls = cu_stub_other_then(child_only(commercial_fields()))
    resp = post(valid_body())
    payload = as_json(resp)

    assert resp.status_code == 200
    # the ordinary gates decide: this extraction is clean, so it is a happy path
    assert payload["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert payload["routerCategory"] == "other", "the router's verdict must stay visible"
    assert any("B2 rescue" in a for a in payload["advisoryFlags"])
    assert payload["writeValues"]["total_invoice_amount"] == 105.0
    # first call goes to the router (analyzer_id=None), second bypasses it
    assert calls == [None, "generalinvoice"], calls

    # three blobs now: the router response, the decision, and the re-analysis
    names = sorted(fake_container.blobs)
    assert len(names) == 3, names
    assert any(n.endswith("-raw-rescue.json") for n in names)
    raw = json.loads(fake_container.blobs[[n for n in names if n.endswith("-raw.json")][0]])
    assert raw == router_only_other(), "-raw.json must still be the ROUTER response"

    row = ledger_row(fake_table)
    assert row["RoutingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert row["Status"] == "Extracted"
    assert row["FailedStage"] == ""
    # A rescued row now looks like any other happy path, so RouterCategory is the
    # only way to find these afterwards (RouterCategory eq 'other').
    assert row["RouterCategory"] == "other", row.get("RouterCategory")


def test_b2_rescue_unusable_response_keeps_the_reject(fake_table, fake_container,
                                                      cu_stub_other_then):
    """A rescue whose response yields no child fields leaves the reject standing.
    Note this exercises the *no-fields floor* in apply_b2_rescue, not the exception
    guard: gates.evaluate is defensive and returns REVIEW_NO_CHILD_EXTRACTION for
    this shape rather than raising (probed over 11 malformed shapes, none raise)."""
    cu_stub_other_then({"contents": "not-a-list-at-all"})
    resp = post(valid_body())
    payload = as_json(resp)

    assert resp.status_code == 200
    assert payload["routingDecision"] == gates.REJECT_B2_OTHER_CATEGORY
    assert ledger_row(fake_table)["Status"] == "Extracted"
    assert ledger_row(fake_table)["FailedStage"] == ""


def test_b2_rescue_exception_does_not_become_a_500(fake_table, fake_container,
                                                   monkeypatch, cu_stub_other_then):
    """The exception guard itself. No known CU shape makes gates.evaluate raise, so
    this forces one: without the try/except around the rescue's evaluate the error
    reaches the outer handler, which returns 500 AND releases the A1 claim to
    Failed -- strictly worse than the reject the rescue was meant to improve."""
    cu_stub_other_then(child_only(commercial_fields()))
    real_evaluate = gates.evaluate
    seen = []

    def flaky(full, *args, **kwargs):
        seen.append(1)
        if len(seen) == 2:            # the rescue's evaluate, not the router's
            raise ValueError("unexpected CU shape")
        return real_evaluate(full, *args, **kwargs)

    monkeypatch.setattr(gates, "evaluate", flaky)
    resp = post(valid_body())
    payload = as_json(resp)

    assert len(seen) == 2, "the rescue's evaluate was reached"
    assert resp.status_code == 200, "a raising rescue must not become a 500"
    assert payload["routingDecision"] == gates.REJECT_B2_OTHER_CATEGORY
    row = ledger_row(fake_table)
    assert row["Status"] == "Extracted", "the A1 claim must not be released"
    assert row["FailedStage"] == ""


def test_b2_rescue_cu_failure_keeps_the_reject(fake_table, fake_container, monkeypatch):
    """Same floor when the second CU call itself raises."""
    calls = []

    def fake_binary(content, file_name=None, analyzer_id=None, timeout=None):
        calls.append(analyzer_id)
        if len(calls) == 1:
            return router_only_other()
        raise RuntimeError("CU exploded on the rescue")

    monkeypatch.setattr(cu_client, "analyze_binary", fake_binary)
    resp = post(valid_body())

    assert resp.status_code == 200
    assert as_json(resp)["routingDecision"] == gates.REJECT_B2_OTHER_CATEGORY
    assert len(calls) == 2, "the rescue was attempted"
    # only the two normal blobs -- no rescue blob when there is no rescue result
    assert len(fake_container.blobs) == 2, sorted(fake_container.blobs)


def test_non_other_documents_make_exactly_one_cu_call(fake_table, fake_container,
                                                      monkeypatch):
    """The rescue must not fire on the 1,019 happy-path documents."""
    calls = []

    def fake_binary(content, file_name=None, analyzer_id=None, timeout=None):
        calls.append(analyzer_id)
        return cu_result(commercial_fields())

    monkeypatch.setattr(cu_client, "analyze_binary", fake_binary)
    resp = post(valid_body())

    assert as_json(resp)["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert calls == [None], "no second CU call on a document the router accepted"
    assert len(fake_container.blobs) == 2, "no rescue blob on a normal run"
