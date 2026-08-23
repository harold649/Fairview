"""Tattoo revenue attribution by artist.

Two naming conventions run in parallel and both are live:

* a plain ``TATTOO`` item, with the artist identified by the order's employee;
* a per-artist SKU (``TATTOO JAMES``, ``TATTOO-Genesis``), where the suffix
  names the artist.

**Order Employee Name is the attribution key in both cases.** Where a
per-artist SKU is used, its suffix is a cross-check only. A suffix that
disagrees with the employee is an exception for a human to resolve, not a
choice for this module to make -- picking a side silently is how a day's
revenue ends up under the wrong artist with two sources that both looked fine.

The all-employee and per-artist Employee Sales exports are cross-checks too.
They are all-category totals, so a non-zero total for an artist does not mean
tattoo revenue, and they are never the primary source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from .config import Config
from .money import ZERO, total
from .sources.line_items import LineItem

#: Exact ``TATTOO``, or ``TATTOO`` followed by a separator. The separator
#: matters: a bare substring would catch ``TATTOO AFTERCARE BALM``.
TATTOO_SKU = re.compile(r"^TATTOO(?:[\s\-]+(?P<suffix>.+))?$", re.IGNORECASE)


@dataclass
class ArtistTotal:
    name: str
    amount: Decimal
    line_count: int
    order_ids: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class AttributionIssue:
    kind: str                    # unlisted_artist | suffix_mismatch |
                                 # suffix_not_roster | missing_employee
    order_id: str
    employee: str
    item_name: str
    amount: Decimal
    detail: str

    @property
    def blocking(self) -> bool:
        """Every attribution issue blocks. None of them has a safe default."""
        return True


@dataclass
class Attribution:
    artists: list[ArtistTotal] = field(default_factory=list)
    issues: list[AttributionIssue] = field(default_factory=list)
    lines: list[LineItem] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return total(a.amount for a in self.artists)

    @property
    def unattributed(self) -> Decimal:
        return total(i.amount for i in self.issues)

    def by_name(self) -> dict[str, Decimal]:
        return {a.name: a.amount for a in self.artists}


def is_tattoo_line(item_name: str) -> bool:
    return bool(TATTOO_SKU.match((item_name or "").strip()))


def sku_suffix(item_name: str) -> Optional[str]:
    m = TATTOO_SKU.match((item_name or "").strip())
    if not m:
        return None
    suffix = (m.group("suffix") or "").strip()
    return suffix or None


def attribute(lines: Iterable[LineItem], config: Config) -> Attribution:
    """Attribute every TATTOO line to a roster artist.

    Any line that cannot be attributed with both sources agreeing is recorded
    as an issue and left out of the artist totals, so that the shortfall shows
    up as a variance instead of being absorbed into someone else's number.
    """
    result = Attribution()
    buckets: dict[str, ArtistTotal] = {}

    for li in lines:
        if not is_tattoo_line(li.item_name):
            continue
        result.lines.append(li)
        employee = (li.employee or "").strip()
        suffix = sku_suffix(li.item_name)

        if not employee:
            result.issues.append(AttributionIssue(
                "missing_employee", li.order_id, "", li.item_name, li.amount,
                "TATTOO line with no Order Employee Name. The employee is the "
                "attribution key; a SKU suffix cannot stand in for it."))
            continue

        canonical = config.canonical_artist(employee)
        if canonical is None:
            retired = config.retirement_note(employee)
            detail = (f"{employee} is not on the confirmed tattoo roster. "
                      + (retired or "Confirm the roster before posting; do not "
                                    "auto-attribute an unlisted name."))
            result.issues.append(AttributionIssue(
                "unlisted_artist", li.order_id, employee, li.item_name,
                li.amount, detail))
            continue

        if suffix is not None:
            suffix_artist = config.canonical_artist(suffix)
            if suffix_artist is None:
                retired = config.retirement_note(suffix)
                result.issues.append(AttributionIssue(
                    "suffix_not_roster", li.order_id, employee, li.item_name,
                    li.amount,
                    f"SKU suffix {suffix!r} is not a roster artist. "
                    + (retired or "Treat as an unlisted artist -- a per-artist "
                                  "SKU does not add someone to the roster.")))
                continue
            if suffix_artist != canonical:
                result.issues.append(AttributionIssue(
                    "suffix_mismatch", li.order_id, employee, li.item_name,
                    li.amount,
                    f"SKU says {suffix_artist}, order employee says "
                    f"{canonical}. Two sources disagree; resolve at the shop "
                    f"rather than choosing one here."))
                continue

        bucket = buckets.get(canonical)
        if bucket is None:
            bucket = buckets[canonical] = ArtistTotal(canonical, ZERO, 0)
        bucket.amount += li.amount
        bucket.line_count += 1
        if li.order_id not in bucket.order_ids:
            bucket.order_ids.append(li.order_id)

    result.artists = sorted(buckets.values(), key=lambda a: a.name)
    return result


def verify(attribution: Attribution, declared_total: Decimal) -> Optional[str]:
    """Re-derive the artist totals from source rows and compare.

    This is the backstop against a transcription error: the artist figures and
    the parent total can agree with each other while both are wrong in the
    same direction, which is exactly what a single sum-check misses.
    """
    attributed_lines = total(
        li.amount for li in attribution.lines
    ) - attribution.unattributed
    recomputed = total(a.amount for a in attribution.artists)
    if recomputed != declared_total:
        return (f"Tattoo parent total {declared_total} does not equal the sum "
                f"of per-artist lines {recomputed}.")
    if recomputed != attributed_lines:
        return (f"Per-artist lines sum to {recomputed} but the attributable "
                f"TATTOO source rows sum to {attributed_lines}.")
    return None
