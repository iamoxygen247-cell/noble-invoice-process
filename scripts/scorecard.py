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
result) and by verify_fn.py (which builds the same rows from the Function's decision
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
import html
import json
import os
import re
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


def write_text_atomic(path: Path, text: str) -> None:
    """Write text to a file via a temp file plus atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = temp_path_for(path)
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(text)
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


# The analyzer prompt's rule for invoice_description: "Hard limit: under 44 characters
# including spaces and punctuation". Enforced in the prompt only -- nothing truncates -- so
# this gate is the one place the rule is checked programmatically.
INVOICE_DESCRIPTION_MAX_CHARS = 44


def invoice_description_gate(value: Any) -> str:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return "not critical - empty"
    chars = len(str(value))
    if chars < INVOICE_DESCRIPTION_MAX_CHARS:
        return "pass"
    return f"warning: {chars} chars >= {INVOICE_DESCRIPTION_MAX_CHARS}"


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


# -----------------------------------------------------------------------------
# HTML scorecard (same matrix model as the CSV writer, rendered as a table)
# -----------------------------------------------------------------------------
#
# The HTML file is both the human view (a styled table) and its own data store: the
# matrix (headers, ordered row labels, cells) is embedded as JSON in a <script> tag so
# an --append run can read it back and add a new column, exactly like the CSV writer.

SCORECARD_DATA_ID = "scorecard-data"
_SCORECARD_DATA_RE = re.compile(
    r'<script[^>]*id="' + re.escape(SCORECARD_DATA_ID) + r'"[^>]*>(.*?)</script>',
    re.DOTALL,
)

_HTML_STYLE = """\
body { font-family: "Segoe UI", system-ui, sans-serif; margin: 1.25rem; color: #1f2328; }
h1 { font-size: 1.1rem; margin: 0 0 0.35rem; }
.legend { font-size: 0.78rem; color: #6b7280; margin: 0 0 0.9rem; }
.legend .chip { display: inline-block; padding: 1px 8px; border-radius: 10px; font-weight: 600; margin-right: 0.5rem; }
.chip.pass { background: #e6f4ea; color: #137333; }
.chip.review { background: #fce8e6; color: #c5221f; }
.chip.warn { background: #fef7e0; color: #b06000; }
table { border-collapse: separate; border-spacing: 0; font-size: 0.86rem; font-variant-numeric: tabular-nums; }
th, td { border-right: 1px solid #d7dbe0; border-bottom: 1px solid #d7dbe0; padding: 5px 11px; text-align: left; vertical-align: top; white-space: pre-wrap; }
thead th { background: #eef1f5; font-weight: 600; position: sticky; top: 0; z-index: 2; border-top: 1px solid #d7dbe0; }
thead th:first-child { border-left: 1px solid #d7dbe0; }
tbody th { background: #fff; font-weight: 600; position: sticky; left: 0; z-index: 1; border-left: 1px solid #d7dbe0; max-width: 24rem; }
/* heavier rule starts each field group; sub-rows keep the thin rule so a field reads as one block */
tr.group th, tr.group td { border-top: 2px solid #c2c8d0; }
/* band the label column of alternate groups so a field is easy to track across run columns */
tr.band th { background: #f3f6fb; }
/* a field's confidence/gate/check rows: indented and de-emphasised, attribute name only */
tr.sub th { font-weight: 400; color: #6b7280; padding-left: 1.9rem; }
tr.sub th::before { content: "↳"; color: #aab0b9; margin-right: 0.35rem; }
/* outcome highlights come last so they win over the group banding */
td.pass { background: #e6f4ea; color: #137333; }
td.review { background: #fce8e6; color: #c5221f; font-weight: 600; }
td.warn { background: #fef7e0; color: #b06000; }
"""


def _cell_class(label: str, value: str) -> str:
    """CSS class for a cell, so gate/decision outcomes stand out at a glance."""
    v = (value or "").strip()
    if label.endswith(".gate"):
        if v == "pass":
            return "pass"
        if v.upper().startswith("REVIEW"):
            return "review"
        if v.lower().startswith("warning"):
            return "warn"
        return ""
    if label == "routing_decision":
        u = v.upper()
        if u.startswith("HAPPY"):
            return "pass"
        if u.startswith("REVIEW") or u.startswith("REJECT"):
            return "review"
    return ""


def render_scorecard_html(
    headers: List[str],
    order: List[str],
    cellmap: Dict[str, List[str]],
    title: str = "Invoice scorecard",
) -> str:
    """Render the matrix (headers, ordered row labels, cells) to a standalone HTML page
    with the data embedded for round-tripping on the next --append run."""
    width = len(headers)
    data = {"headers": headers, "order": order, "cells": cellmap}
    # Keep the JSON from prematurely closing the <script>: "</" -> "<\/" stays valid JSON.
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    head_cells = "".join(f"<th>{html.escape(h)}</th>" for h in headers)

    # Group the flat row list so each field reads as a block: the top-level field row
    # (e.g. vendor_name) followed by its indented sub-rows (.confidence/.gate/...). A row
    # is a sub-row when its "stem.attr" stem matches the field directly above it; sub-rows
    # show only the attr. A heavier rule (tr.group) separates one field from the next, and
    # alternate groups get a banded label column so a field is easy to track across runs.
    body_rows: List[str] = []
    current_parent: Optional[str] = None
    group_index = -1
    for key in order:
        stem = key.split(".", 1)[0]
        is_child = "." in key and stem == current_parent
        if not is_child:
            current_parent = key
            group_index += 1

        row_cls = ["sub" if is_child else "group"]
        if group_index % 2 == 1:
            row_cls.append("band")
        label_text = key.split(".", 1)[1] if is_child else key

        cells = cellmap.get(key, [""] * width)
        if len(cells) < width:
            cells = cells + [""] * (width - len(cells))
        tds = []
        for cell in cells:
            raw = str(cell)
            cls = _cell_class(key, raw)
            cls_attr = f' class="{cls}"' if cls else ""
            # Display-only: one item per line for ' | '-joined cells (e.g. review_reasons).
            text = html.escape(raw).replace(" | ", "\n")
            tds.append(f"<td{cls_attr}>{text}</td>")
        body_rows.append(
            f'<tr class="{" ".join(row_cls)}"><th>{html.escape(label_text)}</th>{"".join(tds)}</tr>'
        )

    legend = (
        '<p class="legend">'
        '<span class="chip pass">pass</span>'
        '<span class="chip review">review</span>'
        '<span class="chip warn">warning</span>'
        "Indented ↳ rows belong to the field above them (confidence, gate, and checks)."
        "</p>"
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>\n{_HTML_STYLE}</style>\n"
        f'<script type="application/json" id="{SCORECARD_DATA_ID}">{data_json}</script>\n'
        "</head>\n<body>\n"
        f"<h1>{html.escape(title)}</h1>\n"
        f"{legend}\n"
        "<table>\n<thead>\n"
        f"<tr><th>field</th>{head_cells}</tr>\n"
        "</thead>\n<tbody>\n"
        + "\n".join(body_rows)
        + "\n</tbody>\n</table>\n</body>\n</html>\n"
    )


def read_scorecard_html_matrix(
    path: Path,
) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """Recover (headers, order, cellmap) from the JSON the HTML writer embedded.
    Returns empty structures if the file has no recoverable data block."""
    text = path.read_text(encoding="utf-8")
    match = _SCORECARD_DATA_RE.search(text)
    if not match:
        return [], [], {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return [], [], {}
    headers = [str(h) for h in data.get("headers", [])]
    order = [str(k) for k in data.get("order", [])]
    cellmap = {str(k): [str(c) for c in v] for k, v in (data.get("cells") or {}).items()}
    return headers, order, cellmap


def write_scorecard_html(
    path: Path,
    pairs: List[Tuple[str, str]],
    column_header: str,
    append: bool,
) -> None:
    """Write this run as a column in an HTML matrix table (the HTML analogue of
    write_scorecard_matrix).

    append=False (default): overwrite to a fresh single-column table (field + this run).
    append=True: read the data embedded in the existing HTML and add this run as a new
    rightmost column. An --append against a missing/empty (or data-less) file falls back
    to a fresh single-column table.
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
        headers, order, cellmap = read_scorecard_html_matrix(path)
        column_header = unique_header(column_header, headers)
        width = len(headers)

        merged_order = list(order)
        for key in this_keys:
            if key not in cellmap and key not in merged_order:
                merged_order.append(key)

        new_cellmap: Dict[str, List[str]] = {}
        for key in merged_order:
            left = cellmap.get(key, [""] * width)
            if len(left) < width:
                left = left + [""] * (width - len(left))
            new_cellmap[key] = left + [this_map.get(key, "")]

        write_text_atomic(path, render_scorecard_html(headers + [column_header], merged_order, new_cellmap))
        return

    # Overwrite (default), or an --append against a missing/empty file: fresh table.
    new_cellmap = {key: [this_map[key]] for key in this_keys}
    write_text_atomic(path, render_scorecard_html([column_header], this_keys, new_cellmap))
