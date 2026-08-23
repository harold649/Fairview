"""Allocating each order's revenue and tax to the tender that paid for it.

A QuickBooks Desktop Sales Receipt carries one payment method, so the day is
posted as one receipt per tender group. That is not a formatting choice: it is
what makes the card receipt tie to the closeout batch (and therefore to the
bank deposit) while cash sits on its own in Undeposited Funds until someone
walks it to the bank.

Most orders have a single payment and allocate trivially. Split-tender orders
are apportioned across their payments by payment amount, using largest-
remainder so that no cent is created or lost. Tips are not apportioned -- each
payment carries its own tip and it goes to that payment's group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from .money import ZERO, allocate, q, total
from .sources.line_items import LineItem
from .sources.payments import (CARD, CASH, DEPOSIT_REDEEMED, GIFTCARD, OTHER,
                               Payment)


@dataclass
class AllocatedLine:
    """One source line, or one slice of it, assigned to a tender group."""

    line: LineItem
    group: str
    amount: Decimal
    share: Decimal              # fraction of the source line, for the audit trail
    payment_ids: list[str] = field(default_factory=list)
    split: bool = False

    @property
    def order_id(self) -> str:
        return self.line.order_id


@dataclass
class UnpaidOrder:
    order_id: str
    amount: Decimal
    employee: str
    reason: str


@dataclass
class Allocation:
    lines: list[AllocatedLine] = field(default_factory=list)
    unpaid: list[UnpaidOrder] = field(default_factory=list)
    tips: dict[str, Decimal] = field(default_factory=dict)
    payment_totals: dict[str, Decimal] = field(default_factory=dict)
    split_orders: list[str] = field(default_factory=list)
    overpaid_orders: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)

    def groups(self) -> list[str]:
        order = [CARD, CASH, GIFTCARD, DEPOSIT_REDEEMED, OTHER]
        present = {al.group for al in self.lines} | set(self.tips) | set(self.payment_totals)
        ranked = [g for g in order if g in present]
        ranked += sorted(g for g in present if g not in order)
        return ranked

    def lines_for(self, group: str) -> list[AllocatedLine]:
        return [al for al in self.lines if al.group == group]

    def revenue_for(self, group: str) -> Decimal:
        return total(al.amount for al in self.lines_for(group))

    @property
    def allocated_total(self) -> Decimal:
        return total(al.amount for al in self.lines)


def allocate_orders(lines: Iterable[LineItem],
                    payments_by_order: dict[str, list[Payment]]) -> Allocation:
    """Assign every line item to one or more tender groups.

    An order with no successful payment is not allocated at all -- it is
    recorded as unpaid and reported. Silently folding an unpaid order into the
    cash figure is how a drawer count stops matching for reasons nobody can
    reconstruct a week later.
    """
    result = Allocation()
    by_order: dict[str, list[LineItem]] = {}
    for li in lines:
        by_order.setdefault(li.order_id, []).append(li)

    for order_id, order_lines in by_order.items():
        payments = [p for p in payments_by_order.get(order_id, []) if p.successful]
        order_total = total(li.amount for li in order_lines)

        if not payments:
            result.unpaid.append(UnpaidOrder(
                order_id, order_total,
                order_lines[0].employee if order_lines else "",
                "No successful payment for this order in the Payments export "
                "(open order, or paid outside Clover)."))
            continue

        groups = _group_weights(payments)
        if len(groups) == 1:
            group = next(iter(groups))
            ids = [p.payment_id for p in payments]
            for li in order_lines:
                result.lines.append(
                    AllocatedLine(li, group, li.amount, Decimal(1), ids))
        else:
            result.split_orders.append(order_id)
            names = list(groups)
            weights = [groups[g] for g in names]
            for li in order_lines:
                shares = allocate(li.amount, weights)
                for name, part in zip(names, shares):
                    if part == ZERO and li.amount != ZERO:
                        continue
                    ids = [p.payment_id for p in payments if p.group == name]
                    share = (part / li.amount) if li.amount else Decimal(0)
                    result.lines.append(
                        AllocatedLine(li, name, part, share, ids, split=True))

        # Payments cover revenue plus tax plus tips. A payment total that
        # exceeds what the order can account for means money arrived with no
        # line behind it -- an appointment deposit taken on the same order, or
        # cashback. Both are real and both need naming; neither is absorbed.
        paid_ex_tip = total(p.amount for p in payments)
        result.payment_totals.setdefault(order_id, paid_ex_tip)

        for p in payments:
            result.tips[p.group] = result.tips.get(p.group, ZERO) + p.tip

    return result


def _group_weights(payments: list[Payment]) -> dict[str, Decimal]:
    """Weight per tender group for an order, by payment amount excluding tips.

    A zero-amount payment (a tip-only card swipe) still registers its group so
    its tip lands somewhere, but carries no weight in the revenue split.
    """
    weights: dict[str, Decimal] = {}
    for p in payments:
        weights[p.group] = weights.get(p.group, ZERO) + p.amount
    if all(w == ZERO for w in weights.values()) and weights:
        # Every payment was tip-only. Put the (zero) revenue on the first group.
        first = next(iter(weights))
        return {first: Decimal(1)}
    return {g: w for g, w in weights.items() if w != ZERO} or weights
