"""
test_diagnostics.py — regression tests for the diagnostics blob sidecar and the
ledger failure/duration stamps.

Guards the diagnostics contract:
  * D1: a happy-path run persists the raw CU result and the decision JSON as two
    blobs under the RowKey prefix, and stamps their paths + AnalyzerId +
    CuDurationMs on the Extracted ledger row.
  * D2: a diagnostics outage is best-effort — the request still returns the full
    decision, the ledger pointers are empty strings, no exception escapes.
  * D3: a CU analyze failure stamps FailedStage/LastError (truncated) on the row
    while leaving Status=Received, so a stuck row is diagnosable; a later
    successful re-run (after the A1 lease) clears the stamps.

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
from datetime import datetime, timedelta, timezone

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

    row = ledger_row(fake_table)
    assert row["Status"] == "Received", "a CU failure must leave the row resumable"
    assert row["FailedStage"] == "cu_analyze"
    assert row["LastError"].startswith("CU exploded")
    assert len(row["LastError"]) == 1024
    assert fake_container.blobs == {}, "no diagnostics blobs for a failed analyze"


def test_failure_stamp_write_error_still_returns_502(fake_table, fake_container, monkeypatch):
    monkeypatch.setattr(cu_client, "analyze_binary",
                        lambda content, file_name=None: (_ for _ in ()).throw(RuntimeError("CU down")))
    monkeypatch.setattr(ledger, "upsert",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("table down")))

    resp = post(valid_body())
    assert resp.status_code == 502
    assert "CU down" in as_json(resp)["error"]


def test_successful_rerun_clears_failure_stamps(fake_table, fake_container, cu_stub, monkeypatch):
    # First run: CU fails, row gets the stamps.
    monkeypatch.setattr(cu_client, "analyze_binary",
                        lambda content, file_name=None: (_ for _ in ()).throw(RuntimeError("blip")))
    assert post(valid_body()).status_code == 502
    row = ledger_row(fake_table)
    assert row["FailedStage"] == "cu_analyze"

    # Age the row past the A1 lease so the retry may reclaim it, restore CU.
    row["LastUpdatedUtc"] = datetime.now(timezone.utc) - timedelta(seconds=700)
    monkeypatch.setattr(cu_client, "analyze_binary",
                        lambda content, file_name=None: cu_result(commercial_fields()))

    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    assert payload["status"] == "Extracted"

    row = ledger_row(fake_table)
    assert row["FailedStage"] == ""
    assert row["LastError"] == ""
    assert row["RawResultBlob"].endswith("-raw.json")
