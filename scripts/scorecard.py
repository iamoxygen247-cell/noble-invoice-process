#!/usr/bin/env python3
r"""
scorecard.py - shared, stdlib-only output helpers for the Noble invoice harnesses.

This is the SINGLE SOURCE for two things both test harnesses need:

  1. Atomic file writers (write_json_atomic, write_rows_atomic) so a result.json or
     scorecard.csv is never left half-written if a run is interrupted.
  2. The matrix scorecard machinery (write_scorecard_matrix and its parsers) - the
     "column 0 = field labels, one column per run, append a run as a new rightmost
     column, migrate a legacy vertical file on first append" format.

Every function here is lifted verbatim from step24_test.py. It has NO third-party or
Azure dependency (csv / os / json / datetime / pathlib only), so it can be imported
by step24_test.py (which builds its scorecard rows from the raw Content Understanding
result) and by local_test.py (which builds the same rows from the Function's decision
JSON). Each harness owns its own row-BUILDING (the input shapes differ); the row-
WRITING is identical and lives here.

Contract for write_scorecard_matrix:
    pairs:         list[(row_label, cell_value_str)] for THIS run, in row order.
    column_header: the header for this run's column (a label or UTC run id).
    append=False:  overwrite the file to a fresh two-column matrix (field + this run).
    append=True:   add this run as a new rightmost column; a legacy vertical-section
                   file is migrated to a matrix on first append (prior runs preserved
                   as their own columns).
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------------------------------------------------------
# Stable output file helpers
# -----------------------------------------------------------------------------


def run_id_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def temp_path_for(path: Path) -> Path:
    """Return a temp file path in the same directory for atomic replacement."""
    suffix = path.suffix or ".tmp"
    return path.with_name(f".{path.stem}.{os.getpid()}.tmp{suffix}")


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON safely whether or not the target file already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = temp_path_for(path)
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_all_rows(path: Path) -> List[List[str]]:
    """Read every row of a CSV file. Returns [] if the file is missing or empty."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return [row for row in csv.reader(f)]


def write_rows_atomic(path: Path, out_rows: List[List[str]]) -> None:
    """Write all rows to CSV via a temp file plus atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = temp_path_for(path)
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for row in out_rows:
                writer.writerow(row)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# -----------------------------------------------------------------------------
# Cell formatting / small gates (shape-agnostic; shared by both harnesses)
# -----------------------------------------------------------------------------


def csv_scalar(value: Any) -> str:
    """Serialize a value into one CSV cell."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def count_words(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    return len([word for word in value.replace("/", " ").split() if word.strip()])


def invoice_description_gate(value: Any) -> str:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return "not critical - empty"
    words = count_words(value)
    if words <= 15:
        return "pass"
    return f"warning: {words} words > 15"


# -----------------------------------------------------------------------------
# Matrix header helpers
# -----------------------------------------------------------------------------


def derive_column_header(run_label: Optional[str], run_id: str) -> str:
    """Column header for this run: the run label if given, otherwise the UTC run id."""
    label = (run_label or "").strip()
    return label if label else run_id


def unique_header(candidate: str, existing: List[str]) -> str:
    """Return candidate, suffixed #2, #3, ... if it collides with an existing column."""
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}#{n}" in existing:
        n += 1
    return f"{candidate}#{n}"


# -----------------------------------------------------------------------------
# Legacy vertical-section migration
# -----------------------------------------------------------------------------


def looks_like_old_vertical(rows: List[List[str]]) -> bool:
    """The legacy stacked format is identified by the presence of a 'record_start' row."""
    return any(row and row[0] == "record_start" for row in rows)


def parse_vertical_sections(rows: List[List[str]]) -> List[List[Tuple[str, str]]]:
    """
    Parse the legacy stacked scorecard into one (key, value) list per run section.
    Scaffolding rows (header, record_start, record_end, blank separators) are dropped.
    """
    sections: List[List[Tuple[str, str]]] = []
    current: Optional[List[Tuple[str, str]]] = None
    for row in rows:
        if not row:
            continue
        key = row[0]
        value = row[1] if len(row) > 1 else ""
        if key == "field" and value == "value":
            continue
        if key == "record_start":
            current = []
            continue
        if key == "record_end":
            if current is not None:
                sections.append(current)
                current = None
            continue
        if key == "" and value == "":
            continue
        if current is not None:
            current.append((key, value))
    if current:  # tolerate a trailing section that was never closed with record_end
        sections.append(current)
    return sections


def matrix_from_sections(
    sections: List[List[Tuple[str, str]]],
) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """Convert parsed legacy sections into (headers, ordered_keys, key_to_cells)."""
    headers: List[str] = []
    order: List[str] = []
    seen = set()
    maps: List[Dict[str, str]] = []
    for i, section in enumerate(sections):
        section_map: Dict[str, str] = {}
        for key, value in section:
            section_map[key] = value  # within a section, last value wins
            if key not in seen:
                seen.add(key)
                order.append(key)
        maps.append(section_map)
        raw_header = section_map.get("run_id_utc") or f"run_{i + 1}"
        headers.append(unique_header(raw_header, headers))
    cellmap: Dict[str, List[str]] = {
        key: [maps[i].get(key, "") for i in range(len(sections))] for key in order
    }
    return headers, order, cellmap


def parse_matrix(
    rows: List[List[str]],
) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """Parse an existing matrix scorecard into (headers, ordered_keys, key_to_cells)."""
    if not rows:
        return [], [], {}
    header = rows[0]
    headers = header[1:]
    width = len(headers)
    order: List[str] = []
    cellmap: Dict[str, List[str]] = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        key = row[0]
        cells = row[1:]
        if len(cells) < width:
            cells = cells + [""] * (width - len(cells))
        elif len(cells) > width:
            cells = cells[:width]
        if key in cellmap:
            continue  # defensive: ignore duplicate row labels
        cellmap[key] = cells
        order.append(key)
    return headers, order, cellmap


# -----------------------------------------------------------------------------
# Matrix writer (the entry point harnesses call)
# -----------------------------------------------------------------------------


def write_scorecard_matrix(
    path: Path,
    pairs: List[Tuple[str, str]],
    column_header: str,
    append: bool,
) -> None:
    """
    Write this run as a column in a matrix CSV (column 0 holds the field labels).

    append=False (default): overwrite to a fresh two-column matrix (field + this run).
    append=True: add this run as a new rightmost column. If the existing file is in the
    legacy stacked vertical-section format, it is migrated to a matrix first so prior
    runs are preserved as their own columns.
    """
    # Deduplicate this run's keys defensively; keep first-seen order, last value wins.
    this_keys: List[str] = []
    this_map: Dict[str, str] = {}
    for key, value in pairs:
        if key not in this_map:
            this_keys.append(key)
        this_map[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)

    if append and path.exists() and path.stat().st_size > 0:
        rows = read_all_rows(path)
        if looks_like_old_vertical(rows):
            headers, order, cellmap = matrix_from_sections(parse_vertical_sections(rows))
        else:
            headers, order, cellmap = parse_matrix(rows)

        column_header = unique_header(column_header, headers)
        width = len(headers)

        merged_order = list(order)
        for key in this_keys:
            if key not in cellmap and key not in merged_order:
                merged_order.append(key)

        out_rows: List[List[str]] = [["field"] + headers + [column_header]]
        for key in merged_order:
            left = cellmap.get(key, [""] * width)
            if len(left) < width:
                left = left + [""] * (width - len(left))
            out_rows.append([key] + left + [this_map.get(key, "")])
        write_rows_atomic(path, out_rows)
        return

    # Overwrite (default), or an --append against a missing/empty file: fresh matrix.
    out_rows = [["field", column_header]]
    for key in this_keys:
        out_rows.append([key, this_map[key]])
    write_rows_atomic(path, out_rows)
