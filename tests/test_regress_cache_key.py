"""
test_regress_cache_key.py — the golden-corpus cache key must not depend on line endings.

scripts/regress.py keys its cached CU responses on a hash of the analyzer definition
file. That hash used to be taken over the RAW BYTES, which made it flip between two
values for a definition that had not changed:

  .gitattributes declares `* text=auto eol=lf`, so git rewrites the analyzer to LF on
  every checkout, stash, or discard -- while a Windows editor, or any Python write with
  newline=None, puts CRLF back. On create-generalinvoice-analyzer.json the two forms
  differ by ~230 bytes.

Each flip silently orphaned the cache and forced a full 108-call re-roll against
Content Understanding. It happened three times on 2026-08-28; one of them landed inside
a `git commit`, where the pre-commit hook started re-rolling and blew a five-minute
timeout mid-commit, and ~30 CU calls were spent before the kill.

Normalising to LF before hashing makes the key depend on the definition -- the thing
that actually changes extraction behaviour -- rather than on which tool last wrote the
file. This test pins that.

No Azure dependency: pure hashing of bytes on disk.
"""

from __future__ import annotations

import importlib.util
import pathlib

_REPO = pathlib.Path(__file__).resolve().parent.parent
_REGRESS = _REPO / "scripts" / "regress.py"


def _load_regress():
    """Import scripts/regress.py by path -- it is a script, not an installed module."""
    spec = importlib.util.spec_from_file_location("regress_under_test", _REGRESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_analyzer_hash_ignores_line_endings(tmp_path):
    regress = _load_regress()
    body = b'{\n  "description": "line one",\n  "note": "line two"\n}\n'

    lf = tmp_path / "lf.json"
    lf.write_bytes(body)
    crlf = tmp_path / "crlf.json"
    crlf.write_bytes(body.replace(b"\n", b"\r\n"))

    assert lf.read_bytes() != crlf.read_bytes(), "fixture is not exercising the difference"
    assert regress._analyzer_hash(lf) == regress._analyzer_hash(crlf), (
        "analyzer hash still depends on line endings: a git checkout would orphan the "
        "cache and force a full CU re-roll"
    )


def test_analyzer_hash_still_tracks_real_content_changes(tmp_path):
    """The normalisation must not make the hash blind to an actual prompt edit."""
    regress = _load_regress()
    a = tmp_path / "a.json"
    a.write_bytes(b'{"description": "take the no-grant column"}\n')
    b = tmp_path / "b.json"
    b.write_bytes(b'{"description": "take the regular-grant column"}\n')

    assert regress._analyzer_hash(a) != regress._analyzer_hash(b), (
        "hash no longer distinguishes different definitions -- a prompt edit would "
        "silently reuse the previous definition's cached reads"
    )


def test_the_real_analyzer_files_hash_identically_either_way(tmp_path):
    """Belt and braces on the definitions actually shipped."""
    regress = _load_regress()
    for name in ("create-generalinvoice-analyzer.json", "create-router-analyzer.json"):
        src = _REPO / "analyzers" / name
        if not src.is_file():
            continue
        raw = src.read_bytes()
        flipped = tmp_path / name
        flipped.write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        assert regress._analyzer_hash(src) == regress._analyzer_hash(flipped), (
            f"{name}: hash differs between LF and CRLF"
        )


if __name__ == "__main__":  # standalone run, mirroring the sibling suites
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
