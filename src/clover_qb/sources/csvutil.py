"""Shared CSV reading helpers for Clover exports."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterator, Optional

#: Clover writes a UTF-8 BOM on most exports.
ENCODING = "utf-8-sig"


def norm_header(name: str) -> str:
    """Normalize a header cell for matching: lowercase, single-spaced."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def read_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a Clover CSV into (headers, rows).

    Rows are keyed by the raw header text; use :func:`pick` to fetch a column
    by any of several accepted spellings.
    """
    with open(path, newline="", encoding=ENCODING) as fh:
        reader = csv.reader(fh)
        rows = [r for r in reader]
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    out = []
    for raw in rows[1:]:
        if not any(cell.strip() for cell in raw):
            continue
        record = {}
        for i, header in enumerate(headers):
            record[header] = raw[i].strip() if i < len(raw) else ""
        out.append(record)
    return headers, out


def read_raw(path: str | Path) -> list[list[str]]:
    """Read a CSV with no header assumption (section-structured exports)."""
    with open(path, newline="", encoding=ENCODING) as fh:
        return [[c.strip() for c in row] for row in csv.reader(fh)]


def pick(row: dict[str, str], *candidates: str, default: Optional[str] = "") -> str:
    """Fetch a column by any accepted spelling, matching on normalized headers."""
    wanted = {norm_header(c) for c in candidates}
    for key, value in row.items():
        if norm_header(key) in wanted:
            return value
    if default is None:
        raise KeyError(f"none of {candidates} found in {list(row)}")
    return default


def has_columns(headers: list[str], *required: str) -> list[str]:
    """Return the required columns that are missing from ``headers``."""
    present = {norm_header(h) for h in headers}
    return [c for c in required if norm_header(c) not in present]


def truthy(value: str) -> bool:
    """Clover writes booleans as ``true``/``false``/``Yes``/``No``/blank."""
    return (value or "").strip().lower() in ("true", "yes", "y", "1")
