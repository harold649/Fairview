"""``Tax Report`` -- the authoritative tax figure for the day.

Authoritative because it covers every payment method. The closeout's tax
covers card only, so it is never the tax anchor.

The export is a label/value report rather than a rectangular table, and Clover
has shipped it with several different label spellings (English and French).
The parser scans for known labels rather than fixed row offsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..money import ZERO, parse_money, total
from .csvutil import norm_header, read_raw

_MONEY = re.compile(r"[-(]?\$?\s*[\d,]+\.\d{2}\)?")

LABELS = {
    "applicable_sales": ("applicable sales", "taxable sales", "ventes taxables",
                         "net sales taxable"),
    "no_tax_sales": ("no tax 0", "no tax", "non taxable sales", "exempt sales",
                     "ventes non taxables"),
    "net_taxes": ("net taxes", "total tax", "total taxes", "taxes nettes"),
    "gst": ("gst", "tps", "gst 5", "tps 5"),
    "qst": ("qst", "tvq", "qst 9 975", "tvq 9 975"),
    "tax_removed": ("tax removed", "taxe retiree", "taxe retirée"),
}


@dataclass
class TaxReport:
    path: Path
    applicable_sales: Decimal = ZERO
    no_tax_sales: Decimal = ZERO
    gst: Decimal = ZERO
    qst: Decimal = ZERO
    net_taxes: Decimal = ZERO
    tax_removed: Decimal = ZERO
    tax_removed_present: bool = False
    raw_labels: dict[str, Decimal] = field(default_factory=dict)

    @property
    def net_sales(self) -> Decimal:
        """Taxable plus exempt sales -- the Clover-side revenue anchor."""
        return self.applicable_sales + self.no_tax_sales

    @property
    def total_tax(self) -> Decimal:
        return self.gst + self.qst


def _label_key(text: str) -> str | None:
    key = norm_header(text)
    if not key:
        return None
    for field_name, spellings in LABELS.items():
        for spelling in spellings:
            if key == spelling or key.startswith(spelling + " ") or \
                    key.replace(" ", "") == spelling.replace(" ", ""):
                return field_name
    return None


def load(path: str | Path) -> TaxReport:
    """Parse the Tax Report.

    A label whose row carries no parseable money value is recorded as present
    but zero rather than skipped -- a "Tax Removed" line at $0.00 still means
    the terminal was overridden and still gets surfaced.
    """
    rows = read_raw(path)
    report = TaxReport(Path(path))
    for row in rows:
        if not row:
            continue
        # The label may not be in column 0 on every export variant.
        for i, cell in enumerate(row):
            key = _label_key(cell)
            if not key:
                continue
            value = ZERO
            for candidate in row[i + 1:]:
                if _MONEY.search(candidate or ""):
                    value = parse_money(candidate)
                    break
            report.raw_labels[cell.strip()] = value
            if key == "tax_removed":
                report.tax_removed_present = True
                report.tax_removed += value
            else:
                setattr(report, key, getattr(report, key) + value)
            break

    if report.net_taxes == ZERO:
        report.net_taxes = report.gst + report.qst
    return report
