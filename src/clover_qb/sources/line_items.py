"""``LineItemsExport`` -- the per-line revenue detail. Primary source.

Every figure the pipeline posts traces back to a row in this file. The other
five exports are anchors and cross-checks; none of them may substitute for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from ..money import ZERO, parse_money, total
from .csvutil import pick, read_rows, truthy
from .identity import gate


@dataclass
class LineItem:
    """One line of one order, as exported."""

    row_no: int
    order_id: str
    order_date: str
    employee: str
    item_name: str
    item_id: str
    amount: Decimal
    quantity: Decimal
    refunded: bool
    payment_state: str
    note: str = ""
    tax_removed: bool = False

    @property
    def is_refund_line(self) -> bool:
        """Two-field refund test: the flag, or a refunded payment state."""
        return self.refunded or "refund" in (self.payment_state or "").lower()

    @property
    def is_open_order(self) -> bool:
        state = (self.payment_state or "").strip().lower()
        return state in ("open", "unpaid", "partially paid", "pending")


@dataclass
class LineItemsFile:
    path: Path
    lines: list[LineItem]
    excluded_refunds: list[LineItem] = field(default_factory=list)
    open_orders: list[LineItem] = field(default_factory=list)

    @property
    def net_sales(self) -> Decimal:
        """Sum of every postable line. This is the day's revenue anchor."""
        return total(li.amount for li in self.lines)

    def order_ids(self) -> set[str]:
        return {li.order_id for li in self.lines}

    def by_order(self) -> dict[str, list[LineItem]]:
        out: dict[str, list[LineItem]] = {}
        for li in self.lines:
            out.setdefault(li.order_id, []).append(li)
        return out


def load(path: str | Path) -> LineItemsFile:
    """Parse LineItemsExport, gating on headers first.

    Refunded lines and open/unpaid orders are separated out rather than
    dropped: both are reported, so a line never disappears silently between
    the export and the receipt.
    """
    gate(path, "line_items")
    _, rows = read_rows(path)

    lines: list[LineItem] = []
    refunds: list[LineItem] = []
    open_orders: list[LineItem] = []

    for n, row in enumerate(rows, start=2):     # row 1 is the header
        order_id = pick(row, "Order ID", "OrderID").strip()
        item_name = pick(row, "Item Name", "Name").strip()
        if not order_id and not item_name:
            continue
        li = LineItem(
            row_no=n,
            order_id=order_id,
            order_date=pick(row, "Order Date", "Date", "Created Time"),
            employee=pick(row, "Order Employee Name", "Employee Name",
                          "Employee").strip(),
            item_name=item_name,
            item_id=pick(row, "Item ID", "ItemID", "Item Id").strip(),
            amount=parse_money(pick(row, "Item Total", "Total", "Price")),
            quantity=parse_money(pick(row, "Quantity", "Qty") or "1"),
            refunded=truthy(pick(row, "Refunded", "Is Refunded")),
            payment_state=pick(row, "Order Payment State", "Payment State",
                               "Payment Status"),
            note=pick(row, "Note", "Item Note", "Order Note"),
            tax_removed=truthy(pick(row, "Tax Removed", "Is Tax Removed")),
        )
        if li.is_refund_line:
            refunds.append(li)
        elif li.is_open_order:
            open_orders.append(li)
        else:
            lines.append(li)

    return LineItemsFile(Path(path), lines, refunds, open_orders)
