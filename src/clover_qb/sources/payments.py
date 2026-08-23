"""``Payments`` -- how each order was actually paid.

This is what makes tender-split Sales Receipts possible: it carries the tender
type, the card brand, the amount and the tip for every payment, keyed by Order
ID. Only ``Result == SUCCESS`` rows are real money; declines and voids are
kept for the audit trail and excluded from every figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..money import ZERO, parse_money, total
from .csvutil import pick, read_rows
from .identity import gate

# Tender groups. One Sales Receipt is produced per group, which is what lets
# the card receipt tie to the closeout batch while cash sits separately in
# Undeposited Funds waiting to be walked to the bank.
CARD = "CARD"
CASH = "CASH"
GIFTCARD = "GIFTCARD"
DEPOSIT_REDEEMED = "DEPOSIT_REDEEMED"
OTHER = "OTHER"

GROUP_ORDER = (CARD, CASH, GIFTCARD, DEPOSIT_REDEEMED, OTHER)


@dataclass
class Payment:
    row_no: int
    order_id: str
    payment_id: str
    tender: str
    card_brand: str
    amount: Decimal          # excludes the tip
    tip: Decimal
    result: str
    employee: str
    date: str = ""

    @property
    def successful(self) -> bool:
        return (self.result or "").strip().upper() == "SUCCESS"

    @property
    def gross(self) -> Decimal:
        """What actually hit the tender: sale plus tip."""
        return self.amount + self.tip

    @property
    def group(self) -> str:
        return classify_tender(self.tender, self.card_brand)


def classify_tender(tender: str, card_brand: str = "") -> str:
    """Map a Clover tender to a receipt group.

    Card brand is checked first: a card payment is a card payment whatever the
    tender column calls it.
    """
    t = (tender or "").strip().upper()
    brand = (card_brand or "").strip()
    if "DEPOSIT" in t:
        # "Deposit Received" is a prior deposit being drawn down, not new
        # money. It never belongs in the card or cash figure.
        return DEPOSIT_REDEEMED
    if "GIFT" in t:
        return GIFTCARD
    if brand:
        return CARD
    if "CASH" in t:
        return CASH
    if any(k in t for k in ("CREDIT", "DEBIT", "INTERAC", "CARD", "VISA",
                            "MASTERCARD", "AMEX", "AMERICAN EXPRESS")):
        return CARD
    return OTHER


@dataclass
class PaymentsFile:
    path: Path
    payments: list[Payment]
    rejected: list[Payment] = field(default_factory=list)

    def successful(self) -> list[Payment]:
        return self.payments

    def by_order(self) -> dict[str, list[Payment]]:
        out: dict[str, list[Payment]] = {}
        for p in self.payments:
            out.setdefault(p.order_id, []).append(p)
        return out

    def group_totals(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in self.payments:
            out[p.group] = out.get(p.group, ZERO) + p.gross
        return out

    def tips_by_group(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in self.payments:
            out[p.group] = out.get(p.group, ZERO) + p.tip
        return out

    @property
    def tips(self) -> Decimal:
        return total(p.tip for p in self.payments)

    def card_brand_totals(self) -> dict[str, tuple[int, Decimal]]:
        """Per-brand count and gross, for the closeout cross-check."""
        out: dict[str, tuple[int, Decimal]] = {}
        for p in self.payments:
            if p.group != CARD or not p.card_brand:
                continue
            count, amount = out.get(p.card_brand, (0, ZERO))
            out[p.card_brand] = (count + 1, amount + p.gross)
        return out

    @property
    def card_total(self) -> Decimal:
        return total(p.gross for p in self.payments if p.group == CARD)


def load(path: str | Path) -> PaymentsFile:
    gate(path, "payments")
    _, rows = read_rows(path)

    good: list[Payment] = []
    bad: list[Payment] = []
    for n, row in enumerate(rows, start=2):
        order_id = pick(row, "Order ID", "OrderID").strip()
        if not order_id:
            continue
        p = Payment(
            row_no=n,
            order_id=order_id,
            payment_id=pick(row, "Payment ID", "ID", "Transaction ID").strip(),
            tender=pick(row, "Tender", "Tender Type", "Payment Type").strip(),
            card_brand=pick(row, "Card Brand", "Card Type").strip(),
            amount=parse_money(pick(row, "Amount", "Payment Amount")),
            tip=parse_money(pick(row, "Tip Amount", "Tip", "Tips")),
            result=pick(row, "Result", "Status", "State").strip(),
            employee=pick(row, "Order Employee Name", "Employee Name",
                          "Employee").strip(),
            date=pick(row, "Date", "Created Time", "Payment Date"),
        )
        (good if p.successful else bad).append(p)
    return PaymentsFile(Path(path), good, bad)
