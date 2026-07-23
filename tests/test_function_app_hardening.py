"""
test_function_app_hardening.py — request-validation, CU-timeout, and ledger-truth
regression tests for the decision engine's HTTP route.

Guards the hardening fixes:
  * F1: a malformed request must 400 BEFORE any ledger write, so it can never
    claim the A1 slot and lock out a corrected retry for the lease duration.
  * F2: invalid base64 is a 400 validation error, never a 502 "CU failed".
  * F3: fieldThreshold outside (0, 1] is rejected; unparseable values fall back.
  * F4: the CU analyze poller is capped by a timeout (cu_client._poll_result).
  * F5: when the post-CU ledger write fails, the response status reports the
    row's true state (Received), plus ledgerWriteError.
  * H1: both harnesses default their threshold to field_policy.THRESHOLD.

Offline: the route is invoked directly via the v2 programming model
(FunctionBuilder.build().get_user_function()), the ledger runs against an
in-memory fake table (the REAL ledger.py code paths execute), and CU is stubbed
at the cu_client module boundary. No Function host, no network.

Run under pytest (the project standard), from the repo root:
    .\\.venv\\Scripts\\python.exe -m pytest
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
for _cand in (_REPO / "scripts", _REPO / "functionapp"):
    if str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

import azure.functions as func  # noqa: E402
from azure.core.exceptions import (  # noqa: E402
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

import cu_client  # noqa: E402
import field_policy  # noqa: E402
import function_app  # noqa: E402
import gates  # noqa: E402
import ledger  # noqa: E402
import test as verify_fn  # noqa: E402  -- scripts/verify_fn.py was renamed to scripts/test.py

THRESHOLD = field_policy.THRESHOLD

# The raw HTTP handler behind the @app.route decorator.
_HANDLER = function_app.process_invoice.build().get_user_function()

SOURCE_ID = "0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77"
PDF_BYTES = b"%PDF-1.4 fake invoice bytes"
PDF_B64 = base64.b64encode(PDF_BYTES).decode("ascii")


# --- CU result builders (same shapes as test_harness_scorecard.py) ------------


def fstr(value, conf):
    return {"valueString": value, "confidence": conf}


def fnum(value, conf):
    return {"valueNumber": value, "confidence": conf}


def fdate(value, conf):
    return {"valueDate": value, "confidence": conf}


def cu_result(fields, category="general_invoice", analyzer="generalinvoice"):
    return {
        "contents": [
            {"segments": [{"category": category}]},
            {"analyzerId": analyzer, "category": category, "fields": fields},
        ]
    }


def commercial_fields():
    return {
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


# --- in-memory Table Storage fake (exercises the real ledger.py code) ---------


class FakeTable:
    """Mimics the TableClient surface ledger.py touches, with the same atomic
    semantics: create_entity raises ResourceExistsError on a duplicate key,
    get_entity raises ResourceNotFoundError on a miss, and update_entity honours
    etag + match_condition (ResourceModifiedError on a mismatch), so the A1
    re-claim race is exercised for real."""

    def __init__(self):
        self.rows = {}
        self.write_calls = []  # every create/upsert/update, in order
        self._versions = {}

    def _etag(self, key):
        return f'W/"{self._versions[key]}"'

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        try:
            entity = dict(self.rows[key])
        except KeyError:
            raise ResourceNotFoundError("entity not found")
        entity["odata.etag"] = self._etag(key)
        return entity

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.write_calls.append(("create", key))
        if key in self.rows:
            raise ResourceExistsError("entity already exists")
        self.rows[key] = dict(entity)
        self._versions[key] = self._versions.get(key, 0) + 1
        return {"etag": self._etag(key)}

    def upsert_entity(self, entity, mode=None):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.write_calls.append(("upsert", key))
        self.rows.setdefault(key, {}).update(entity)
        self._versions[key] = self._versions.get(key, 0) + 1

    def update_entity(self, entity, mode=None, etag=None, match_condition=None):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.write_calls.append(("update", key))
        if key not in self.rows:
            raise ResourceNotFoundError("entity not found")
        if match_condition is not None and etag != self._etag(key):
            raise ResourceModifiedError("etag mismatch")
        self.rows[key].update(entity)
        self._versions[key] += 1
        return {"etag": self._etag(key)}


@pytest.fixture
def fake_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(ledger, "get_table_client", lambda: table)
    monkeypatch.delenv("FIELD_CONFIDENCE_THRESHOLD", raising=False)
    return table


@pytest.fixture
def cu_stub(monkeypatch):
    """Stub cu_client.analyze_binary/analyze_url with a confident commercial
    invoice; records calls so tests can assert CU was (not) reached."""
    calls = []

    def fake_binary(content, file_name=None):
        calls.append(("binary", content, file_name))
        return cu_result(commercial_fields())

    def fake_url(url):
        calls.append(("url", url, None))
        return cu_result(commercial_fields())

    monkeypatch.setattr(cu_client, "analyze_binary", fake_binary)
    monkeypatch.setattr(cu_client, "analyze_url", fake_url)
    return calls


def post(payload):
    body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode("utf-8")
    req = func.HttpRequest(
        method="POST",
        url="http://localhost/api/process-invoice",
        headers={"content-type": "application/json"},
        params={},
        body=body,
    )
    return _HANDLER(req)


def as_json(resp):
    return json.loads(resp.get_body().decode("utf-8"))


def valid_body(**overrides):
    body = {"sourceId": SOURCE_ID, "contentBase64": PDF_B64, "fileName": "invoice1.pdf"}
    body.update(overrides)
    return body


# --- _field_threshold (F3) -----------------------------------------------------


def test_threshold_default_is_policy(monkeypatch):
    monkeypatch.delenv("FIELD_CONFIDENCE_THRESHOLD", raising=False)
    assert function_app._field_threshold({}) == THRESHOLD


@pytest.mark.parametrize("raw,expected", [(0.9, 0.9), ("0.8", 0.8), (1.0, 1.0), (0.001, 0.001)])
def test_threshold_valid_body_values(monkeypatch, raw, expected):
    monkeypatch.delenv("FIELD_CONFIDENCE_THRESHOLD", raising=False)
    assert function_app._field_threshold({"fieldThreshold": raw}) == expected


@pytest.mark.parametrize("raw", [0, 0.0, -0.2, 1.5, 99, "0"])
def test_threshold_out_of_range_raises(monkeypatch, raw):
    monkeypatch.delenv("FIELD_CONFIDENCE_THRESHOLD", raising=False)
    with pytest.raises(ValueError):
        function_app._field_threshold({"fieldThreshold": raw})


def test_threshold_unparseable_body_falls_back(monkeypatch):
    monkeypatch.delenv("FIELD_CONFIDENCE_THRESHOLD", raising=False)
    assert function_app._field_threshold({"fieldThreshold": "abc"}) == THRESHOLD


def test_threshold_env_used_and_range_checked(monkeypatch):
    monkeypatch.setenv("FIELD_CONFIDENCE_THRESHOLD", "0.85")
    assert function_app._field_threshold({}) == 0.85
    # body overrides env
    assert function_app._field_threshold({"fieldThreshold": 0.9}) == 0.9
    # out-of-range / junk env is server config: fall back, never 400
    monkeypatch.setenv("FIELD_CONFIDENCE_THRESHOLD", "7")
    assert function_app._field_threshold({}) == THRESHOLD
    monkeypatch.setenv("FIELD_CONFIDENCE_THRESHOLD", "junk")
    assert function_app._field_threshold({}) == THRESHOLD


# --- _decode_content (F2) --------------------------------------------------------


def test_decode_valid_base64():
    content, url, err = function_app._decode_content({"contentBase64": PDF_B64})
    assert (content, url, err) == (PDF_BYTES, None, None)


def test_decode_tolerates_wrapped_base64():
    wrapped = "\r\n".join(PDF_B64[i:i + 8] for i in range(0, len(PDF_B64), 8)) + "\n"
    content, url, err = function_app._decode_content({"contentBase64": wrapped})
    assert (content, url, err) == (PDF_BYTES, None, None)


@pytest.mark.parametrize("bad", ["!!!not-base64!!!", "AAA", "AA==garbage"])
def test_decode_invalid_base64_is_an_error(bad):
    content, url, err = function_app._decode_content({"contentBase64": bad})
    assert content is None and url is None
    assert err == "contentBase64 is not valid base64"


def test_decode_non_string_content_is_an_error():
    content, url, err = function_app._decode_content({"contentBase64": 123})
    assert err == "contentBase64 must be a base64 string"


def test_decode_url_transport():
    content, url, err = function_app._decode_content({"url": "https://example/blob?sas"})
    assert (content, url, err) == (None, "https://example/blob?sas", None)


@pytest.mark.parametrize("body", [{}, {"contentBase64": ""}, {"contentBase64": "  \n"}, {"url": "  "}])
def test_decode_missing_transport_is_an_error(body):
    content, url, err = function_app._decode_content(body)
    assert content is None and url is None
    assert err == "provide contentBase64 (binary transport) or url"


# --- route: bad requests must not touch the ledger (F1) --------------------------


def test_missing_transport_400_and_no_ledger_write(fake_table, cu_stub):
    resp = post({"sourceId": SOURCE_ID})
    assert resp.status_code == 400
    assert "contentBase64" in as_json(resp)["error"]
    assert fake_table.write_calls == [], "a 400 request must never write a ledger row"
    assert cu_stub == []


def test_invalid_base64_is_400_not_502(fake_table, cu_stub):
    resp = post(valid_body(contentBase64="!!!not-base64!!!"))
    assert resp.status_code == 400
    assert as_json(resp)["error"] == "contentBase64 is not valid base64"
    assert fake_table.write_calls == []
    assert cu_stub == []


def test_bad_threshold_400_and_no_ledger_write(fake_table, cu_stub):
    resp = post(valid_body(fieldThreshold=0))
    assert resp.status_code == 400
    assert "fieldThreshold" in as_json(resp)["error"]
    assert fake_table.write_calls == []
    assert cu_stub == []


def test_retry_after_malformed_request_is_not_locked_out(fake_table, cu_stub):
    """THE F1 regression: a bad request followed by a corrected retry with the
    same sourceId must process normally, not short-circuit as in-progress."""
    bad = post({"sourceId": SOURCE_ID})  # forgot the content
    assert bad.status_code == 400

    good = post(valid_body())
    payload = as_json(good)
    assert good.status_code == 200
    assert payload["alreadyProcessed"] is False
    assert payload["skippedCU"] is False
    assert payload["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert payload["status"] == "Extracted"


# --- route: happy path and A1 semantics (concurrency guard, re-uploads re-process) --


def test_happy_path_extracts_and_records(fake_table, cu_stub):
    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    assert payload["status"] == "Extracted"
    assert payload["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert "ledgerWriteError" not in payload
    assert "writeValues" in payload

    # CU received the decoded bytes, not the base64 text.
    assert cu_stub == [("binary", PDF_BYTES, "invoice1.pdf")]

    # The ledger row went Received (atomic claim) then Extracted.
    pk, rk = ledger.keys_for_source_id(SOURCE_ID)
    row = fake_table.rows[(pk, rk)]
    assert row["Status"] == "Extracted"
    assert row["RoutingDecision"] == gates.HAPPY_PATH_CANDIDATE


def test_url_transport_still_works(fake_table, cu_stub):
    resp = post({"sourceId": SOURCE_ID, "url": "https://example/blob?sas"})
    assert resp.status_code == 200
    assert as_json(resp)["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert cu_stub == [("url", "https://example/blob?sas", None)]


def test_reupload_of_processed_item_reprocesses(fake_table, cu_stub):
    """A1 dedup removed: the same sourceId posted again (a re-uploaded file)
    must run the full pipeline again, not short-circuit."""
    first = post(valid_body())
    assert as_json(first)["alreadyProcessed"] is False

    second = post(valid_body())
    payload = as_json(second)
    assert second.status_code == 200
    assert payload["alreadyProcessed"] is False
    assert payload["skippedCU"] is False
    assert payload["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert payload["status"] == "Extracted"
    assert len(cu_stub) == 2, "the re-upload must run CU again"

    pk, rk = ledger.keys_for_source_id(SOURCE_ID)
    row = fake_table.rows[(pk, rk)]
    assert row["Status"] == "Extracted"
    assert row["RoutingDecision"] == gates.HAPPY_PATH_CANDIDATE


def test_in_flight_item_still_short_circuits(fake_table, cu_stub):
    """The concurrency guard kept: a Received row with no decision, within the
    lease, is another invocation mid-run — a double-fired trigger must skip."""
    ledger.claim(fake_table, SOURCE_ID, Status="Received",
                 FileName="", SharePointUrl="", RoutingDecision="")
    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    assert payload["alreadyProcessed"] is True
    assert payload["skippedCU"] is True
    assert payload["routingDecision"] == "PROCESSING_IN_PROGRESS"
    assert cu_stub == [], "an in-flight item must not reach CU"


def test_reclaim_is_atomic_per_etag(fake_table, cu_stub):
    """Two concurrent invocations read the same decided row: only the first
    etag-conditioned re-claim wins; the loser gets None and must skip."""
    post(valid_body())  # leaves a decided row
    row = ledger.get_row(fake_table, SOURCE_ID)
    etag = ledger.entity_etag(row)
    assert etag
    assert ledger.reclaim(fake_table, SOURCE_ID, etag,
                          Status="Received", RoutingDecision="")
    assert ledger.reclaim(fake_table, SOURCE_ID, etag,
                          Status="Received", RoutingDecision="") is None


def test_non_json_body_is_400(fake_table, cu_stub):
    resp = post(b"this is not json")
    assert resp.status_code == 400
    assert fake_table.write_calls == []


def test_missing_source_id_is_400(fake_table, cu_stub):
    resp = post({"contentBase64": PDF_B64})
    assert resp.status_code == 400
    assert fake_table.write_calls == []


# --- route: ledger-write failure reports the row's true state (F5) ----------------


def test_ledger_write_failure_reports_received(fake_table, cu_stub, monkeypatch):
    def failing_upsert(*args, **kwargs):
        raise RuntimeError("simulated table outage")

    monkeypatch.setattr(ledger, "upsert", failing_upsert)

    resp = post(valid_body())
    payload = as_json(resp)
    assert resp.status_code == 200
    # The decision is still returned for the flow ...
    assert payload["routingDecision"] == gates.HAPPY_PATH_CANDIDATE
    assert "writeValues" in payload
    # ... but status tells the truth: the row never reached Extracted.
    assert payload["status"] == "Received"
    assert "simulated table outage" in payload["ledgerWriteError"]

    pk, rk = ledger.keys_for_source_id(SOURCE_ID)
    assert fake_table.rows[(pk, rk)]["Status"] == "Received"


# --- cu_client: analyze poller timeout (F4) ---------------------------------------


class FakePoller:
    def __init__(self, completes: bool, result_value=None):
        self._completes = completes
        self._result_value = result_value if result_value is not None else {"contents": []}
        self.wait_timeout = "never called"

    def wait(self, timeout=None):
        self.wait_timeout = timeout

    def done(self):
        return self._completes

    def result(self):
        return self._result_value


def test_poll_result_returns_dict_when_done(monkeypatch):
    monkeypatch.delenv("AZURE_CU_TIMEOUT_SECONDS", raising=False)
    poller = FakePoller(completes=True, result_value={"contents": [1]})
    assert cu_client._poll_result(poller) == {"contents": [1]}
    assert poller.wait_timeout == cu_client.DEFAULT_ANALYZE_TIMEOUT_SECONDS


def test_poll_result_raises_timeout_when_not_done(monkeypatch):
    monkeypatch.setenv("AZURE_CU_TIMEOUT_SECONDS", "45")
    poller = FakePoller(completes=False)
    with pytest.raises(TimeoutError, match="45s"):
        cu_client._poll_result(poller)
    assert poller.wait_timeout == 45.0


@pytest.mark.parametrize(
    "env,expected",
    [(None, 100.0), ("45", 45.0), ("junk", 100.0), ("-5", 100.0), ("0", 100.0)],
)
def test_analyze_timeout_env_parsing(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("AZURE_CU_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("AZURE_CU_TIMEOUT_SECONDS", env)
    assert cu_client.analyze_timeout_seconds() == expected


# --- endpoint is required config: no environment baked into the code --------------


def test_endpoint_raises_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("AZURE_CU_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_CU_ENDPOINT"):
        cu_client.endpoint()


def test_endpoint_raises_when_env_var_empty(monkeypatch):
    monkeypatch.setenv("AZURE_CU_ENDPOINT", "")
    with pytest.raises(RuntimeError, match="AZURE_CU_ENDPOINT"):
        cu_client.endpoint()


def test_endpoint_normalizes_trailing_slash(monkeypatch):
    monkeypatch.setenv("AZURE_CU_ENDPOINT", "https://example.services.ai.azure.com")
    assert cu_client.endpoint() == "https://example.services.ai.azure.com/"


# --- harness threshold defaults match the Function's policy (H1) ------------------


@pytest.mark.parametrize("harness", [pytest.param(verify_fn, id="verify_fn")])
def test_harness_default_threshold_matches_policy(harness):
    assert harness.DEFAULT_FIELD_THRESHOLD == THRESHOLD
