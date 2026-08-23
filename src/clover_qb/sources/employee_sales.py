"""``Employee Sales`` -- a cross-check, never a primary source.

The per-employee net sales figure is all-category: a non-zero total for an
artist does not mean tattoo revenue. Tattoo attribution comes from the
LineItemsExport rows and nowhere else. What this file is good for is
independently confirming the day's tips and net sales totals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..money import ZERO, parse_money, total
from .csvutil import norm_header, pick, read_raw, read_rows
from .identity import gate

_NET_SALES = ("net sales", "net sale", "ventes nettes")
_TIPS = ("tips", "tip", "pourboires", "tip amount")


@dataclass
class EmployeeRow:
    employee: str
    net_sales: Decimal = ZERO
    tips: Decimal = ZERO


@dataclass
class EmployeeSales:
    path: Path
    employees: dict[str, EmployeeRow] = field(default_factory=dict)

    @property
    def net_sales(self) -> Decimal:
        return total(e.net_sales for e in self.employees.values())

    @property
    def tips(self) -> Decimal:
        return total(e.tips for e in self.employees.values())


def load(path: str | Path) -> EmployeeSales:
    """Parse the all-employee summary.

    Handles both shapes Clover exports: a rectangular table with an Employee
    column, and a per-employee block of label/value rows.
    """
    p = Path(path)
    headers, rows = read_rows(p)
    keys = {norm_header(h) for h in headers}
    out = EmployeeSales(p)

    if "employee" in keys or "employee name" in keys:
        for row in rows:
            name = pick(row, "Employee", "Employee Name").strip()
            if not name or norm_header(name) in ("total", "totals"):
                continue
            out.employees[name] = EmployeeRow(
                name,
                parse_money(pick(row, "Net sales", "Net Sales", "Net sale")),
                parse_money(pick(row, "Tips", "Tip", "Tip Amount")),
            )
        return out

    # Label/value block form.
    current = ""
    for row in read_raw(p):
        if not any(c.strip() for c in row):
            continue
        label = norm_header(row[0])
        value_cells = [c for c in row[1:] if c.strip()]
        if not value_cells:
            current = row[0].strip()
            out.employees.setdefault(current, EmployeeRow(current))
            continue
        if not current:
            current = "(all employees)"
            out.employees.setdefault(current, EmployeeRow(current))
        if any(label.startswith(k) for k in _NET_SALES):
            out.employees[current].net_sales += parse_money(value_cells[0])
        elif any(label.startswith(k) for k in _TIPS):
            out.employees[current].tips += parse_money(value_cells[0])
    return out
