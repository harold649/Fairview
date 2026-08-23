"""File-identity gate.

A plausible filename is not the same as the right file. Clover exports several
line-item reports whose names differ by a few characters; ``LineItemsExport``
and ``LineItemsGroupedByItem`` are the pair that has actually caused a bad run.
The grouped report aggregates by item across the day and drops Order ID,
Order Employee Name and the per-line refund fields, so artist attribution has
no source at all -- and a single-artist day still looks reconcilable, which is
what makes it dangerous.

Identity is decided by the header row, never by the filename.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .csvutil import has_columns, norm_header, read_rows


class FileIdentityError(Exception):
    """The file supplied is not the export the pipeline needs."""


@dataclass(frozen=True)
class Spec:
    kind: str
    required: tuple[str, ...]
    rejects: tuple[tuple[str, tuple[str, ...]], ...] = ()   # (name, signature)


SPECS: dict[str, Spec] = {
    "line_items": Spec(
        "LineItemsExport",
        ("Order ID", "Order Employee Name", "Item Name", "Item Total"),
        rejects=(("LineItemsGroupedByItem",
                  ("# of Items Sold", "Item Revenue", "Total Revenue")),),
    ),
    "payments": Spec(
        "Payments",
        ("Order ID", "Tender", "Amount", "Result"),
    ),
    "employee_sales": Spec(
        "Employee Sales",
        ("Employee",),
    ),
}


def gate(path: str | Path, kind: str) -> list[str]:
    """Verify ``path`` is the export named by ``kind``. Returns its headers.

    Raises :class:`FileIdentityError` naming the specific export needed. A
    file that fails this gate is a hard stop, never a degraded input to work
    around with a cross-check file.
    """
    spec = SPECS[kind]
    p = Path(path)
    if not p.exists():
        raise FileIdentityError(f"{spec.kind} not found at {p}")
    headers, _ = read_rows(p)
    if not headers:
        raise FileIdentityError(f"{p.name} is empty; expected {spec.kind}")

    present = {norm_header(h) for h in headers}
    for reject_name, signature in spec.rejects:
        if all(norm_header(c) in present for c in signature):
            raise FileIdentityError(
                f"{p.name} is a {reject_name} export, not {spec.kind}. "
                f"It aggregates by item and has no Order ID or Order Employee "
                f"Name, so artist attribution has no source. Supply the "
                f"{spec.kind} export instead -- do not substitute a related "
                f"report."
            )

    missing = has_columns(headers, *spec.required)
    if missing:
        raise FileIdentityError(
            f"{p.name} does not look like {spec.kind}: missing column(s) "
            f"{', '.join(missing)}. Found: {', '.join(headers[:12])}"
        )
    return headers
