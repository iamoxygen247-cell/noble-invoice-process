#!/usr/bin/env python3
r"""
regress.py - golden-corpus regression for the general-invoice analyzer.

Answers one question with a command instead of a manual eyeball: does the
working-tree analyzer prompt still produce the verified values on the corpus of
docs we care about? It provisions the working-tree definition as a throwaway
analyzer, runs each corpus PDF through the SAME decision path the Function uses
(gates.evaluate), and compares the result against a per-doc expectation sidecar.

Corpus: every *.pdf in tests/pre-commit-test/, each with a sibling
<stem>.expected.json (see --update-expected to scaffold one). The folder is
gitignored (real customer invoices); a missing/empty folder is a clean skip so a
fresh clone still passes.

Why it can run on every commit: results are cached content-addressed by
(analyzer definition hash, pdf bytes hash, replicate index). When neither the
prompt nor the PDFs changed, ZERO CU calls happen and the run just re-scores
cached raw JSON offline -- about a second. A code change to gates.py /
field_policy.py also re-scores for free, giving that path coverage at no API cost.

Three outcomes per (doc, field):
  OK       - matches the expectation on every replicate
  WRONG    - disagrees with the expectation
  UNSTABLE - replicates disagree with each other (a coin-flip doc)
Exit is non-zero on any WRONG or UNSTABLE. A fully green run stamps
out/regress/<git-sha>.json, which scripts/create_analyzer.py's prod push gate
requires.

TLS note: this machine sits behind a TLS-inspecting agent that OpenSSL
strict-rejects; truststore is injected below so CU calls verify against the
Windows store (see docs/ai/troubleshooting.md). No REQUESTS_CA_BUNDLE needed.

Examples (PowerShell, from the repo root, after setting AZURE_CU_ENDPOINT +
AZURE_CU_KEY):
    .\.venv\Scripts\python.exe scripts\regress.py
    .\.venv\Scripts\python.exe scripts\regress.py --replicates 5
    .\.venv\Scripts\python.exe scripts\regress.py --update-expected bug_260629_0026
    .\.venv\Scripts\python.exe scripts\regress.py --analyzer-file <path>   # e.g. a control built from git show
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

# The narrative fields carry Traditional Chinese, and this script prints observed
# field values (mismatch rows, the --add scaffold's unstable list). On Windows a
# *redirected* stdout defaults to cp1252, so printing one would raise
# UnicodeEncodeError and bury a real FAIL under an encoding traceback -- and the
# pre-commit hook runs exactly that way. Console stdout is already UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
    pass

# --- TLS: verify against the Windows store, like func/.NET (troubleshooting.md).
os.environ.pop("REQUESTS_CA_BUNDLE", None)
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - truststore is a dev dependency here
    pass

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
_FUNCTIONAPP = _REPO / "functionapp"
if str(_FUNCTIONAPP) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONAPP))

import cu_client  # noqa: E402
import field_policy  # noqa: E402
import gates  # noqa: E402

CORPUS_DIR = _REPO / "tests" / "pre-commit-test"
CACHE_DIR = _REPO / "out" / "regress-cache"
STAMP_DIR = _REPO / "out" / "regress"
DEFAULT_ANALYZER_FILE = _REPO / "analyzers" / "create-generalinvoice-analyzer.json"
# A throwaway id, never the prod 'generalinvoice'. Provisioned from the working
# tree so a run reflects uncommitted edits.
TEST_ANALYZER_ID = "generalinvoicetest"

# Money is compared with a small tolerance; everything else exactly.
MONEY_TOLERANCE = 0.005


# --- hashing / cache ---------------------------------------------------------


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _analyzer_hash(analyzer_file: pathlib.Path) -> str:
    return _sha12(analyzer_file.read_bytes())


def _cache_path(analyzer_hash: str, pdf_hash: str, replicate: int) -> pathlib.Path:
    return CACHE_DIR / analyzer_hash / f"{pdf_hash}-r{replicate}.json"


def _cached_replicates(analyzer_hash: str, pdf_hash: str) -> List[int]:
    """Every replicate index already cached for this (analyzer, pdf), ascending.

    Scoring all of them costs nothing -- they are on disk -- and is strictly more evidence
    than re-scoring the first three. See --all-cached.
    """
    found = []
    for path in (CACHE_DIR / analyzer_hash).glob(f"{pdf_hash}-r*.json"):
        try:
            found.append(int(path.stem.split("-r")[1]))
        except (IndexError, ValueError):  # pragma: no cover - stray file in the cache
            continue
    return sorted(found)


# --- CU provisioning + analysis ----------------------------------------------


class CuRunner:
    """Provisions the test analyzer once (lazily, only if a cache miss forces a
    call) and analyzes PDFs, counting real API calls so the cache can be proven."""

    def __init__(self, analyzer_file: pathlib.Path):
        self.analyzer_file = analyzer_file
        self._client = None
        self._provisioned = False
        self.calls = 0

    def _ensure_client(self):
        if self._client is None:
            self._client = cu_client._client()
        return self._client

    def _ensure_provisioned(self):
        if self._provisioned:
            return
        definition = json.loads(self.analyzer_file.read_text(encoding="utf-8"))
        client = self._ensure_client()
        print(f"  provisioning {TEST_ANALYZER_ID} from {self.analyzer_file.name} ...")
        poller = client.begin_create_analyzer(
            TEST_ANALYZER_ID, resource=definition, allow_replace=True
        )
        poller.result()
        self._provisioned = True

    def analyze(self, pdf_bytes: bytes, file_name: str) -> Dict[str, Any]:
        self._ensure_provisioned()
        client = self._ensure_client()
        content_type = cu_client._guess_content_type(file_name)
        self.calls += 1
        try:
            poller = client.begin_analyze_binary(
                analyzer_id=TEST_ANALYZER_ID,
                binary_input=pdf_bytes,
                content_type=content_type,
            )
        except TypeError:
            poller = client.begin_analyze_binary(
                analyzer_id=TEST_ANALYZER_ID, binary_input=pdf_bytes
            )
        return cu_client._as_dict(poller.result())

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass


def raw_result(
    runner: CuRunner,
    analyzer_hash: str,
    pdf_path: pathlib.Path,
    pdf_hash: str,
    replicate: int,
    force: bool,
) -> Dict[str, Any]:
    """Return the raw CU result for one (pdf, replicate), from cache or a fresh call."""
    cache_path = _cache_path(analyzer_hash, pdf_hash, replicate)
    if cache_path.is_file() and not force:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    result = runner.analyze(pdf_path.read_bytes(), pdf_path.name)
    # Trap #1: prove the result came from the analyzer we provisioned, never a
    # fallback to prod output.
    got = result.get("analyzerId")
    if got != TEST_ANALYZER_ID:
        raise SystemExit(
            f"analyzerId in the result is {got!r}, expected {TEST_ANALYZER_ID!r}: "
            "the run did not hit the test analyzer -- aborting rather than scoring "
            "the wrong analyzer."
        )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


# --- scoring -----------------------------------------------------------------


def _values_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= MONEY_TOLERANCE
    return expected == actual


def _decision(raw: Dict[str, Any], file_name: str) -> Dict[str, Any]:
    return gates.evaluate(
        raw, field_policy.THRESHOLD, TEST_ANALYZER_ID, file_name=file_name
    )


def _observed(decision: Dict[str, Any], section: str, name: str) -> Any:
    """Pull one expected key from a decision. 'fields' holds {value, confidence}
    dicts; writeValues holds plain values; routingDecision is top-level."""
    if section == "routingDecision":
        return decision.get("routingDecision")
    if section == "writeValues":
        return decision.get("writeValues", {}).get(name)
    if section == "fields":
        entry = decision.get("fields", {}).get(name)
        return entry.get("value") if isinstance(entry, dict) else None
    raise ValueError(f"unknown expectation section {section!r}")


def _expectation_checks(expect: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
    """Flatten a sidecar's expect block into (section, name, expected) tuples."""
    checks: List[Tuple[str, str, Any]] = []
    if "routingDecision" in expect:
        checks.append(("routingDecision", "routingDecision", expect["routingDecision"]))
    for section in ("writeValues", "fields"):
        for name, value in (expect.get(section) or {}).items():
            checks.append((section, name, value))
    return checks


def score_doc(
    pdf_path: pathlib.Path,
    expect: Dict[str, Any],
    decisions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One row per checked key: verdict OK / WRONG / UNSTABLE across replicates."""
    rows: List[Dict[str, Any]] = []
    file_name = pdf_path.stem
    for section, name, expected in _expectation_checks(expect):
        observed = [_observed(d, section, name) for d in decisions]
        unstable = any(not _values_equal(observed[0], o) for o in observed[1:])
        matches = all(_values_equal(expected, o) for o in observed)
        if unstable:
            verdict = "UNSTABLE"
        elif matches:
            verdict = "OK"
        else:
            verdict = "WRONG"
        rows.append(
            {
                "doc": file_name,
                "key": f"{section}.{name}",
                "verdict": verdict,
                "expected": expected,
                "observed": observed[0] if not unstable else observed,
            }
        )
    return rows


# --- expectation scaffolding -------------------------------------------------


def add_doc(runner: CuRunner, analyzer_hash: str, pdf: pathlib.Path, replicates: int) -> int:
    """Copy a new PDF into the corpus, then scaffold its expectation sidecar. One
    command to grow the regression footprint after a bug fix."""
    import shutil

    if not pdf.is_file():
        raise SystemExit(f"no such PDF: {pdf}")
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    dest = CORPUS_DIR / pdf.name
    if dest.resolve() != pdf.resolve():
        shutil.copy2(pdf, dest)
        print(f"copied {pdf.name} -> {dest}")
    return update_expected(runner, analyzer_hash, dest.stem, replicates)


def update_expected(runner: CuRunner, analyzer_hash: str, stem: str, replicates: int) -> int:
    pdf_path = CORPUS_DIR / f"{stem}.pdf"
    if not pdf_path.is_file():
        raise SystemExit(f"no such corpus PDF: {pdf_path}")
    pdf_hash = _sha12(pdf_path.read_bytes())
    decisions = [
        _decision(raw_result(runner, analyzer_hash, pdf_path, pdf_hash, r, force=False), stem)
        for r in range(replicates)
    ]

    def stable(section_getter, key):
        vals = [section_getter(d).get(key) for d in decisions]
        return all(_values_equal(vals[0], v) for v in vals[1:]), vals[0], vals

    # Only propose keys that are stable across replicates; an unstable key is
    # listed as a warning, never asserted (asserting a coin-flip guarantees a
    # future red run).
    write0 = decisions[0].get("writeValues", {})
    proposed_wv, unstable = {}, []
    for k in field_policy.WRITE_FIELDS:
        ok, v, vals = stable(lambda d: d.get("writeValues", {}), k)
        if v is None:
            continue
        (proposed_wv.__setitem__(k, v) if ok else unstable.append((k, vals)))

    # The twin fields for total_invoice_amount are the most common bug target;
    # include them when stable so the scaffold matches the hand-written sidecars.
    proposed_fields = {}
    for k in ("total_invoice_amount_extract", "total_invoice_amount_generate"):
        ok, v, _ = stable(lambda d: {n: (e.get("value") if isinstance(e, dict) else None)
                                     for n, e in d.get("fields", {}).items()}, k)
        if ok and v is not None:
            proposed_fields[k] = v

    sidecar = {
        "note": "REVIEW before trusting: observed values, not yet human-verified. "
                "Read the PDF, trim to what you have verified, then keep it.",
        "expect": {
            "routingDecision": decisions[0].get("routingDecision"),
            "writeValues": proposed_wv,
            "fields": proposed_fields,
        },
    }
    out = CORPUS_DIR / f"{stem}.expected.json"
    out.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    print(f"\nwrote {out} ({runner.calls} CU calls).")
    if unstable:
        print("  UNSTABLE across replicates (left OUT of the sidecar -- do not assert these):")
        for k, vals in unstable:
            print(f"    {k}: {vals}")
    print("  NEXT: open the sidecar, delete every value you have not verified against the PDF,")
    print("        keep the bug's field, then commit -- the pre-commit hook will assert it.")
    return 0


# --- reporting ---------------------------------------------------------------


def _load_local_settings() -> None:
    """Seed AZURE_CU_* from functionapp/local.settings.json when not already set.
    Convenience for local/dev and the pre-commit hook; never overrides an env var
    that is already present, so CI or an explicit shell still wins."""
    settings = _FUNCTIONAPP / "local.settings.json"
    if not settings.is_file():
        return
    # utf-8-sig: local.settings.json is often written by PowerShell with a BOM.
    values = json.loads(settings.read_text(encoding="utf-8-sig")).get("Values", {})
    for key in ("AZURE_CU_ENDPOINT", "AZURE_CU_KEY", "AZURE_CU_API_VERSION"):
        if not os.getenv(key) and values.get(key):
            os.environ[key] = values[key]


def _git_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(_REPO), capture_output=True, text=True
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _git_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(_REPO), capture_output=True, text=True
        )
        return bool(out.stdout.strip())
    except Exception:
        return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replicates", type=int, default=3, help="runs per doc (default 3)")
    ap.add_argument("--analyzer-file", type=pathlib.Path, default=DEFAULT_ANALYZER_FILE,
                    help="analyzer definition to test (default: the working-tree general analyzer)")
    ap.add_argument("--add", metavar="PDF", type=pathlib.Path,
                    help="copy a new PDF into the corpus and scaffold its expectation sidecar, then exit "
                         "(the one-command way to grow the corpus after a bug fix)")
    ap.add_argument("--update-expected", metavar="STEM",
                    help="rewrite the expectation sidecar from observed values for a corpus doc already present, then exit")
    ap.add_argument("--force", action="store_true", help="ignore the cache and re-call CU for every replicate")
    ap.add_argument("--all-cached", action="store_true",
                    help="score EVERY replicate already cached for each doc instead of the first "
                         "--replicates. Zero CU calls, and it uses the evidence already paid for: "
                         "a corpus rolled to 14 replicates is otherwise re-scored on 3. Falls back "
                         "to --replicates for a doc with nothing cached")
    ap.add_argument("--no-stamp", action="store_true", help="do not write the out/regress/<sha>.json stamp on green")
    ap.add_argument("--load-local-settings", action="store_true",
                    help="seed AZURE_CU_* env from functionapp/local.settings.json when unset (dev convenience; used by the pre-commit hook)")
    args = ap.parse_args(argv)

    if args.load_local_settings:
        _load_local_settings()

    analyzer_file = args.analyzer_file.resolve()
    if not analyzer_file.is_file():
        raise SystemExit(f"analyzer definition not found: {analyzer_file}")
    analyzer_hash = _analyzer_hash(analyzer_file)
    runner = CuRunner(analyzer_file)

    try:
        if args.add:
            return add_doc(runner, analyzer_hash, args.add, args.replicates)
        if args.update_expected:
            return update_expected(runner, analyzer_hash, args.update_expected, args.replicates)

        if not CORPUS_DIR.is_dir() or not list(CORPUS_DIR.glob("*.pdf")):
            print(f"[skip] no corpus at {CORPUS_DIR} (gitignored; add PDFs + <stem>.expected.json).")
            return 0

        pdfs = sorted(CORPUS_DIR.glob("*.pdf"))
        all_rows: List[Dict[str, Any]] = []
        skipped: List[str] = []
        replicates_scored: Dict[str, int] = {}

        for pdf_path in pdfs:
            sidecar = pdf_path.with_suffix(".expected.json")
            if not sidecar.is_file():
                skipped.append(pdf_path.stem)
                continue
            expect = json.loads(sidecar.read_text(encoding="utf-8")).get("expect", {})
            pdf_hash = _sha12(pdf_path.read_bytes())
            indices = list(range(args.replicates))
            if args.all_cached and not args.force:
                cached = _cached_replicates(analyzer_hash, pdf_hash)
                if cached:
                    indices = cached
            replicates_scored[pdf_path.stem] = len(indices)
            decisions = [
                _decision(
                    raw_result(runner, analyzer_hash, pdf_path, pdf_hash, r, args.force),
                    pdf_path.stem,
                )
                for r in indices
            ]
            all_rows.extend(score_doc(pdf_path, expect, decisions))
    finally:
        runner.close()

    bad = [r for r in all_rows if r["verdict"] != "OK"]
    ok_count = len(all_rows) - len(bad)

    print()
    scored = sum(replicates_scored.values())
    if replicates_scored and len(set(replicates_scored.values())) > 1:
        spread = f"{min(replicates_scored.values())}-{max(replicates_scored.values())}"
    else:
        spread = str(next(iter(replicates_scored.values()), args.replicates))
    print(f"analyzer {analyzer_file.name} [{analyzer_hash}]  x{spread} replicates per doc "
          f"({scored} scored: {runner.calls} CU calls, {scored - runner.calls} cache hits)")
    print(f"checked {len(all_rows)} (doc, field) expectations across {len(pdfs) - len(skipped)} docs: "
          f"{ok_count} OK, {len(bad)} not OK")
    if skipped:
        print(f"  no sidecar (not scored): {', '.join(skipped)}")
    for r in bad:
        print(f"  {r['verdict']:8s} {r['doc']}  {r['key']}: expected {r['expected']!r}, got {r['observed']!r}")

    if bad:
        print("\nRESULT: FAIL")
        return 1

    print("\nRESULT: PASS")
    if not args.no_stamp:
        sha = _git_sha()
        if sha and not _git_dirty():
            STAMP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = STAMP_DIR / f"{sha}.json"
            stamp.write_text(
                json.dumps(
                    {
                        "sha": sha,
                        "analyzerHash": analyzer_hash,
                        "replicates": args.replicates,
                        "docs": len(pdfs) - len(skipped),
                        "checks": len(all_rows),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"stamped {stamp}")
        else:
            print("[note] working tree dirty or no git sha; stamp not written "
                  "(the prod push gate needs a clean-HEAD green run).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
