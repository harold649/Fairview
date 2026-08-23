"""The locked item-name dictionary (``QB_ITEMS.txt``).

The dictionary is the highest-priority classification source after an explicit
session override: an exact item-name match here outranks every keyword rule.
That is what makes it dangerous as well as useful -- a stale entry silently
defeats a newer keyword rule on exactly the items the rule was written to fix
-- so this module also provides the sweep used to find contradicting entries
when a rule changes.

File format (append-only, date-ordered markdown pipe tables, prose between
them). Two table shapes exist and both are read:

    standard    | Item Name | QB Item | Date | Basis |
    correction  | Item Name (exact) | OLD QB Item | NEW QB Item | Confirmed |

In a correction block the effective mapping is the NEW column. Later entries
supersede earlier ones for the same item name; the file is never rewritten in
place, only appended to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from . import taxonomy

_SEPARATOR = re.compile(r"^[\s|:\-=+]+$")
_HEADER_FIRSTS = ("name", "item", "order id", "ex #", "#", "employee", "sku")


def normalize(name: str) -> str:
    """Matching key for an item name: case- and whitespace-insensitive."""
    return re.sub(r"\s+", " ", (name or "").replace("**", "")).strip().upper()


@dataclass(frozen=True)
class Entry:
    """One dictionary row as read from the file."""

    item_name: str
    qb_item: str
    dated: str
    basis: str
    line_no: int
    corrected: bool = False

    @property
    def key(self) -> str:
        return normalize(self.item_name)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def _cells(line: str) -> list[str]:
    parts = line.split("|")
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [_clean(p) for p in parts]


def parse(path: str | Path) -> list[Entry]:
    """Read every table row in the dictionary file, in file order."""
    entries: list[Entry] = []
    qb_col, date_col, basis_col, corrected = 1, 2, 3, False
    with open(path, encoding="utf-8-sig") as fh:
        for n, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if line.count("|") < 2:
                if not line.strip():
                    qb_col, date_col, basis_col, corrected = 1, 2, 3, False
                continue
            if _SEPARATOR.match(line):
                continue
            row = _cells(line)
            if len(row) < 2:
                continue

            first = row[0].lower()
            if first.startswith("item name") or first in _HEADER_FIRSTS:
                # New table: locate the effective QB Item column from the
                # header. A correction block's NEW column wins over its OLD.
                qb_col, corrected = 1, False
                for i, h in enumerate(row):
                    hl = h.lower()
                    if "new" in hl and "qb" in hl:
                        qb_col, corrected = i, True
                        break
                else:
                    for i, h in enumerate(row[1:], start=1):
                        if "qb item" in h.lower():
                            qb_col = i
                            break
                date_col = next(
                    (i for i, h in enumerate(row)
                     if h.lower().startswith(("date", "confirmed"))), -1)
                basis_col = next(
                    (i for i, h in enumerate(row)
                     if h.lower().startswith(("basis", "rationale", "note"))), -1)
                continue

            if qb_col >= len(row) or not row[0] or not row[qb_col]:
                continue
            entries.append(Entry(
                item_name=row[0],
                qb_item=row[qb_col],
                dated=row[date_col] if 0 <= date_col < len(row) else "",
                basis=row[basis_col] if 0 <= basis_col < len(row) else "",
                line_no=n,
                corrected=corrected,
            ))
    return entries


class Dictionary:
    """Effective item-name to QB-item mapping, last dated entry wins."""

    def __init__(self, entries: Iterable[Entry] | None = None):
        self._entries: list[Entry] = list(entries or [])
        self._effective: dict[str, Entry] = {}
        for entry in self._entries:          # file order == chronological order
            self._effective[entry.key] = entry

    @classmethod
    def load(cls, path: str | Path) -> "Dictionary":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"locked dictionary not found: {p}")
        return cls(parse(p))

    @classmethod
    def empty(cls) -> "Dictionary":
        return cls([])

    def __len__(self) -> int:
        return len(self._effective)

    def __contains__(self, item_name: str) -> bool:
        return normalize(item_name) in self._effective

    def lookup(self, item_name: str) -> Optional[Entry]:
        return self._effective.get(normalize(item_name))

    def qb_item(self, item_name: str) -> Optional[str]:
        """Canonical QB item for ``item_name``, resolving deprecated aliases."""
        entry = self.lookup(item_name)
        if entry is None:
            return None
        try:
            return taxonomy.resolve(entry.qb_item).name
        except taxonomy.UnknownItemError:
            return None

    def entries(self) -> list[Entry]:
        return list(self._effective.values())

    def unresolvable(self) -> list[Entry]:
        """Live entries whose QB item is not in the taxonomy at all.

        These are silent failures waiting to happen: the entry matches, the
        lookup returns nothing usable, and the item quietly falls through to a
        keyword rule as if the dictionary had never been consulted.
        """
        return [e for e in self.entries() if not taxonomy.is_known(e.qb_item)]

    # -- sweep --------------------------------------------------------------

    def sweep(self, *, pattern: re.Pattern[str], target: str,
              exclude: re.Pattern[str] | None = None) -> list[Entry]:
        """Live entries matching ``pattern`` whose mapping is not ``target``.

        Call this whenever a classification rule changes. ``exclude`` scopes
        out higher-priority terms: a name containing "stud" that also contains
        "tit" is correctly Titanium, not a contradiction of a stud rule.

        This finds candidates. It does not decide them, and it never edits the
        locked file -- a correction block is drafted for human approval.
        """
        target_name = taxonomy.resolve(target).name
        hits = []
        for entry in self.entries():
            name = entry.item_name
            if not pattern.search(name):
                continue
            if exclude is not None and exclude.search(name):
                continue
            current = self.qb_item(name)
            if current != target_name:
                hits.append(entry)
        return sorted(hits, key=lambda e: e.key)


def render_block(rows: Iterable[tuple[str, str, str]], *, dated: date,
                 basis: str) -> str:
    """Render an append-ready standard block: ``(item_name, qb_item, note)``."""
    rows = list(rows)
    out = [f"\n## {dated.isoformat()} — {basis}\n",
           "| Item Name | QB Item | Date | Basis |",
           "|---|---|---|---|"]
    for name, qb_item, note in rows:
        out.append(f"| {name} | {qb_item} | {dated.isoformat()} | {note or basis} |")
    return "\n".join(out) + "\n"


def render_correction_block(rows: Iterable[tuple[str, str, str]], *,
                            dated: date, basis: str) -> str:
    """Render an append-ready correction block: ``(item_name, old, new)``."""
    rows = list(rows)
    out = [f"\n## {dated.isoformat()} — CORRECTION: {basis}\n",
           "| Item Name (exact) | OLD QB Item | NEW QB Item | Confirmed |",
           "|---|---|---|---|"]
    for name, old, new in rows:
        out.append(f"| {name} | {old} | {new} | {dated.isoformat()} |")
    return "\n".join(out) + "\n"
