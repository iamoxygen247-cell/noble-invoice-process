"""
test_analyzer_contract.py — offline contract tests for analyzers/*.json.

The analyzer definitions drive every extracted field, yet nothing used to read
them under test: the suite passed while a renamed or dropped field silently
produced nulls in production. These assertions derive from gates.py /
field_policy.py / cu_client.py rather than hardcoded lists, so they stay true as
the schema evolves and fail loudly when the JSON and the code disagree.

No Azure dependency: this is pure JSON + code introspection.

Run under pytest (the project standard), from the repo root:
    .\\.venv\\Scripts\\python.exe -m pytest

Or standalone:
    .\\.venv\\Scripts\\python.exe tests\\test_analyzer_contract.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any, Dict

# Same sys.path shim as test_field_policy_gates.py: resolve the modules the
# Function actually runs, under both pytest and a standalone run.
_REPO = pathlib.Path(__file__).resolve().parent.parent
_FUNCTIONAPP = _REPO / "functionapp"
if str(_FUNCTIONAPP) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONAPP))

import cu_client  # noqa: E402  -- imported after the sys.path bootstrap above
import field_policy  # noqa: E402
import gates  # noqa: E402

GENERAL_PATH = _REPO / "analyzers" / "create-generalinvoice-analyzer.json"
ROUTER_PATH = _REPO / "analyzers" / "create-router-analyzer.json"

VALID_METHODS = {"extract", "generate", "classify"}


def _load(path: pathlib.Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def general_fields() -> Dict[str, Any]:
    return _load(GENERAL_PATH)["fieldSchema"]["fields"]


def computed_finals() -> set:
    """The final field names field_policy computes from the twins. These are the
    entries in FIELD_PRINT_ORDER that CU never returns."""
    return {
        getattr(field_policy, name)
        for name in dir(field_policy)
        if name.endswith("_FINAL") and isinstance(getattr(field_policy, name), str)
    }


# --- structure ---------------------------------------------------------------


def test_analyzer_definitions_parse():
    for path in (GENERAL_PATH, ROUTER_PATH):
        assert path.is_file(), f"missing analyzer definition: {path}"
        _load(path)
    assert general_fields(), "general analyzer declares no fields"


def test_every_field_has_a_valid_method_and_description():
    for name, spec in general_fields().items():
        method = spec.get("method")
        assert method in VALID_METHODS, f"{name}: bad method {method!r}"
        assert (spec.get("description") or "").strip(), f"{name}: empty description"
        assert (spec.get("type") or "").strip(), f"{name}: missing type"


# --- the analyzer schema and the code agree ----------------------------------


def test_field_set_matches_field_print_order():
    """CU fields + the finals field_policy computes == exactly FIELD_PRINT_ORDER.

    Catches a field renamed, added or dropped in the analyzer JSON without the
    matching code change -- the failure mode that yields a silent null in prod.
    """
    cu_names = set(general_fields())
    declared = set(gates.FIELD_PRINT_ORDER)
    covered = cu_names | computed_finals()

    missing = declared - covered
    extra = covered - declared
    assert not missing, (
        "in gates.FIELD_PRINT_ORDER but produced by neither the analyzer nor "
        f"field_policy: {sorted(missing)}"
    )
    assert not extra, (
        "produced but absent from gates.FIELD_PRINT_ORDER (it would be appended "
        f"unordered to the scorecard): {sorted(extra)}"
    )


def test_every_twin_constant_exists_with_the_right_method():
    """Every *_EXTRACT / *_GENERATE constant in field_policy resolves to a field
    of that method in the analyzer JSON."""
    fields = general_fields()
    for const in sorted(n for n in dir(field_policy) if n.endswith(("_EXTRACT", "_GENERATE"))):
        name = getattr(field_policy, const)
        if not isinstance(name, str):
            continue
        assert name in fields, f"field_policy.{const} = {name!r} is not in the analyzer schema"
        expected = "extract" if const.endswith("_EXTRACT") else "generate"
        actual = fields[name].get("method")
        assert actual == expected, f"{name}: method is {actual!r}, field_policy expects {expected!r}"


def test_twin_pairs_share_a_type():
    fields = general_fields()
    for name, spec in fields.items():
        if not name.endswith("_extract"):
            continue
        twin = name[: -len("_extract")] + "_generate"
        if twin not in fields:  # lone extracts (payment_due_date) are legitimate
            continue
        assert spec.get("type") == fields[twin].get("type"), (
            f"{name} is {spec.get('type')!r} but {twin} is {fields[twin].get('type')!r}; "
            "twin resolution compares them directly"
        )


def test_value_types_match_what_the_pipeline_parses():
    """Money must be number and dates must be date, or resolution/normalisation
    downstream silently receives strings."""
    fields = general_fields()
    expected_types = {
        field_policy.TOTAL_EXTRACT: "number",
        field_policy.TOTAL_GENERATE: "number",
        field_policy.GST_EXTRACT: "number",
        field_policy.GST_GENERATE: "number",
        field_policy.PST_EXTRACT: "number",
        field_policy.PST_GENERATE: "number",
        field_policy.INVOICE_DATE_EXTRACT: "date",
        field_policy.INVOICE_DATE_GENERATE: "date",
        field_policy.BILLING_START_EXTRACT: "date",
        field_policy.BILLING_START_GENERATE: "date",
        field_policy.BILLING_END_EXTRACT: "date",
        field_policy.BILLING_END_GENERATE: "date",
        field_policy.DAYS_EXTRACT: "integer",
        field_policy.DAYS_GENERATE: "integer",
    }
    for name, expected in expected_types.items():
        assert fields[name].get("type") == expected, (
            f"{name}: type is {fields[name].get('type')!r}, expected {expected!r}"
        )


def test_classify_enums_match_field_policy():
    """A classify enum that drifts from the code silently reroutes bills: an
    unknown bill_type falls through to the commercial bucket."""
    fields = general_fields()

    bill_type = set(fields["bill_type"].get("enum") or [])
    assert bill_type == {field_policy.MUNICIPAL, field_policy.COMMERCIAL}, (
        f"bill_type enum {sorted(bill_type)} != "
        f"{sorted({field_policy.MUNICIPAL, field_policy.COMMERCIAL})}"
    )

    sub_type = set(fields["sub_bill_type"].get("enum") or [])
    required = set(field_policy.MUNICIPAL_SUB_TYPES) | {field_policy.SUB_OTHER}
    assert required <= sub_type, (
        f"sub_bill_type enum is missing {sorted(required - sub_type)}; "
        "field_policy maps those labels"
    )

    assert set(fields["is_handwritten"].get("enum") or []) == {"yes", "no"}


# --- prompt hygiene ----------------------------------------------------------


def test_generate_reasoning_steps_are_numbered_sequentially():
    """The generate-method prompts are numbered reasoning procedures. Renumbering
    them by hand during an edit is easy to get wrong, and a duplicated or skipped
    step is invisible in review."""
    step = re.compile(r"\n(\d+)\.")
    for name, spec in general_fields().items():
        if spec.get("method") != "generate":
            continue
        steps = [int(n) for n in step.findall(spec.get("description", ""))]
        if not steps:  # a prose generate prompt is fine
            continue
        assert steps == list(range(1, len(steps) + 1)), (
            f"{name}: reasoning steps are numbered {steps}, expected "
            f"{list(range(1, len(steps) + 1))}"
        )


# --- router ------------------------------------------------------------------


def test_router_child_analyzer_is_the_one_the_code_reads():
    """The router hardwires which child analyzer runs; gates.find_child_content
    looks for that id. If they disagree the function silently falls back to
    matching on category, which is how a scratch-analyzer verification can end up
    reading production output.
    """
    router = _load(ROUTER_PATH)
    categories = router["config"]["contentCategories"]
    child = categories["general_invoice"]["analyzerId"]
    assert child == cu_client.DEFAULT_GENERAL_ANALYZER_ID, (
        f"router routes general_invoice to {child!r} but cu_client defaults to "
        f"{cu_client.DEFAULT_GENERAL_ANALYZER_ID!r}"
    )


def test_router_categories_cover_the_routing_code():
    router = _load(ROUTER_PATH)
    categories = router["config"]["contentCategories"]
    assert "general_invoice" in categories, "router lost the general_invoice category"
    for name, spec in categories.items():
        assert (spec.get("description") or "").strip(), f"router category {name}: empty description"


if __name__ == "__main__":  # standalone run, mirroring the sibling suites
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
