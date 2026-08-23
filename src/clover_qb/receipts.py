"""Building QuickBooks Desktop Sales Receipts from an allocated day.

One receipt per tender group. A Sales Receipt in QuickBooks Desktop carries a
single payment method and a single deposit-to account, and that constraint is
what the whole design leans on:

* the **card** receipt deposits to Undeposited Funds and its total equals the
  Clover closeout batch, so the deposit that QuickBooks offers matches the
  amount the processor actually settles to the bank;
* the **cash** receipt deposits to Undeposited Funds and is deliberately left
  there -- the money is in the drawer, the bank deposit happens on its own day
  and gets recorded then, against the real bank date;
* **tips** are a liability line on whichever receipt collected them, never
  revenue;
* **deposits** taken and redeemed move the Customer Deposits liability rather
  than inflating or deflating the day's sales.

Tax is reported twice on purpose: as Clover charged it (per line, which is
what the customer actually paid) and as QuickBooks will compute it (on the
receipt subtotal). Those two differ by rounding, and the person entering the
receipt needs to know which figure to override to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from . import taxonomy
from .attribution import Attribution
from .classify import Decision
from .config import Config, DEPOSITS_LIABILITY, DEPOSITS_OFF_QB
from .money import ZERO, fmt, q, total
from .sources.payments import (CARD, CASH, DEPOSIT_REDEEMED, GIFTCARD, OTHER)
from .tenders import AllocatedLine, Allocation

GROUP_LABELS = {
    CARD: "Credit / debit card",
    CASH: "Cash",
    GIFTCARD: "Gift certificate redeemed",
    DEPOSIT_REDEEMED: "Appointment deposit applied",
    OTHER: "Other tender",
}


@dataclass
class ReceiptLine:
    qb_item: str
    description: str
    amount: Decimal
    tax_code: str
    gst: Decimal = ZERO          # as Clover charged it, per source line
    qst: Decimal = ZERO
    line_count: int = 0
    artist: Optional[str] = None

    @property
    def is_revenue(self) -> bool:
        return taxonomy.resolve(self.qb_item).kind == "revenue"


@dataclass
class SalesReceipt:
    receipt_id: str
    group: str
    date: str
    customer: str
    qb_class: str
    payment_method: str
    deposit_to: str
    lines: list[ReceiptLine] = field(default_factory=list)
    memo: str = ""
    notes: list[str] = field(default_factory=list)
    tendered: Decimal = ZERO         # what the Payments export says arrived
    residual: Decimal = ZERO         # tendered less what the receipt accounts for

    # -- figures ------------------------------------------------------------

    @property
    def revenue(self) -> Decimal:
        return total(l.amount for l in self.lines if l.is_revenue)

    @property
    def taxable_base(self) -> Decimal:
        return total(l.amount for l in self.lines
                     if l.tax_code == taxonomy.TAX_S)

    @property
    def exempt_base(self) -> Decimal:
        return total(l.amount for l in self.lines
                     if l.tax_code == taxonomy.TAX_E and l.is_revenue)

    @property
    def gst(self) -> Decimal:
        """GST as Clover charged it: the sum of the per-line amounts."""
        return total(l.gst for l in self.lines)

    @property
    def qst(self) -> Decimal:
        return total(l.qst for l in self.lines)

    @property
    def tax(self) -> Decimal:
        return self.gst + self.qst

    @property
    def gst_qb(self) -> Decimal:
        """GST as QuickBooks will compute it: rate on the receipt subtotal."""
        return q(self.taxable_base * taxonomy.GST_RATE)

    @property
    def qst_qb(self) -> Decimal:
        return q(self.taxable_base * taxonomy.QST_RATE)

    @property
    def tax_override_needed(self) -> bool:
        return (self.gst_qb, self.qst_qb) != (self.gst, self.qst)

    @property
    def tips(self) -> Decimal:
        return total(l.amount for l in self.lines
                     if l.qb_item == taxonomy.TIPS_PAYABLE)

    @property
    def liabilities(self) -> Decimal:
        return total(l.amount for l in self.lines if not l.is_revenue)

    @property
    def subtotal(self) -> Decimal:
        return total(l.amount for l in self.lines)

    @property
    def total(self) -> Decimal:
        """What the receipt is worth: every line plus the tax charged."""
        return self.subtotal + self.tax

    def line_for(self, qb_item: str, artist: Optional[str] = None) -> Optional[ReceiptLine]:
        for line in self.lines:
            if line.qb_item == qb_item and line.artist == artist:
                return line
        return None


def build_receipts(allocation: Allocation, decisions: dict[str, Decision],
                   attribution: Attribution, config: Config, *,
                   date_iso: str, tendered_by_group: dict[str, Decimal],
                   deposit_intake_by_group: Optional[dict[str, Decimal]] = None,
                   ) -> list[SalesReceipt]:
    """Produce one Sales Receipt per tender group.

    ``decisions`` is keyed by item name and supplies the QB item and tax code.
    ``attribution`` supplies the per-artist split of tattoo revenue -- tattoo
    lines are never aggregated into a single anonymous total, because the
    artist split is the whole point of tracking it.
    """
    deposit_intake_by_group = deposit_intake_by_group or {}
    receipts: list[SalesReceipt] = []
    date_compact = date_iso.replace("-", "")

    for group in allocation.groups():
        lines = allocation.lines_for(group)
        tips = allocation.tips.get(group, ZERO)
        intake = deposit_intake_by_group.get(group, ZERO)
        if not lines and tips == ZERO and intake == ZERO:
            continue

        receipt = SalesReceipt(
            receipt_id=f"{_prefix(config)}-{date_compact}-{group}",
            group=group,
            date=date_iso,
            customer=config.customer,
            qb_class=config.qb_class,
            payment_method=GROUP_LABELS.get(group, group.title()),
            deposit_to=_deposit_to(group, config),
            memo=f"{config.location} daily sales {date_iso} - "
                 f"{GROUP_LABELS.get(group, group)}",
            tendered=q(tendered_by_group.get(group, ZERO)),
        )

        _add_revenue_lines(receipt, lines, decisions, attribution, config)
        _add_deposit_line(receipt, lines, decisions, config, intake)
        _add_tips_line(receipt, tips, config)
        _annotate(receipt, config)

        receipt.lines.sort(key=lambda l: (taxonomy.sort_key(l.qb_item),
                                          l.artist or ""))
        receipt.residual = q(receipt.tendered - receipt.total)
        receipts.append(receipt)

    return receipts


# --- line construction -----------------------------------------------------

def _add_revenue_lines(receipt: SalesReceipt, lines: list[AllocatedLine],
                       decisions: dict[str, Decision],
                       attribution: Attribution, config: Config) -> None:
    """Aggregate allocated source lines into receipt lines.

    Tattoo is keyed by artist so each artist gets a line of their own;
    everything else is keyed by QB item.
    """
    from .attribution import is_tattoo_line
    from .classify import normalize

    buckets: dict[tuple[str, Optional[str]], ReceiptLine] = {}
    attributed_orders = {
        (li.order_id, li.item_name): a.name
        for a in attribution.artists
        for li in attribution.lines
        if li.order_id in a.order_ids and config.canonical_artist(li.employee) == a.name
    }

    for al in lines:
        decision = decisions.get(normalize(al.line.item_name))
        if decision is None or not decision.postable:
            continue                       # held lines never reach a receipt
        qb_item = decision.qb_item
        item = taxonomy.resolve(qb_item)

        if item.name == taxonomy.CUSTOMER_DEPOSITS:
            continue                       # handled by _add_deposit_line

        artist = None
        if item.per_artist:
            artist = attributed_orders.get((al.line.order_id, al.line.item_name))
            if artist is None:
                artist = config.canonical_artist(al.line.employee)
            if artist is None:
                # Unattributable tattoo revenue never lands on a receipt. It
                # is reported as an attribution issue and shows up as a
                # variance, which is the point.
                continue

        key = (item.name, artist)
        line = buckets.get(key)
        if line is None:
            line = buckets[key] = ReceiptLine(
                qb_item=item.name,
                description=artist or item.description,
                amount=ZERO,
                tax_code=decision.tax_code,
                artist=artist,
            )
        line.amount += al.amount
        line.line_count += 1
        tax = taxonomy.compute_tax(al.amount, decision.tax_code)
        line.gst += tax.gst
        line.qst += tax.qst

    receipt.lines.extend(buckets.values())


def _add_deposit_line(receipt: SalesReceipt, lines: list[AllocatedLine],
                      decisions: dict[str, Decision], config: Config,
                      extra_intake: Decimal) -> None:
    """Appointment deposits taken today, under whichever policy is in force."""
    from .classify import normalize

    intake = extra_intake
    for al in lines:
        decision = decisions.get(normalize(al.line.item_name))
        if decision and decision.qb_item == taxonomy.CUSTOMER_DEPOSITS:
            intake += al.amount
    intake = q(intake)
    if intake == ZERO:
        return

    if config.deposit_policy == DEPOSITS_LIABILITY:
        receipt.lines.append(ReceiptLine(
            qb_item=taxonomy.CUSTOMER_DEPOSITS,
            description="Appointment deposit taken",
            amount=intake,
            tax_code=taxonomy.TAX_E,
        ))
        receipt.notes.append(
            f"Deposit intake {fmt(intake)} posted to "
            f"{config.accounts.customer_deposits} as a liability. The receipt "
            f"total is the full amount tendered, so it still ties to the batch.")
    else:
        receipt.notes.append(
            f"Deposit intake {fmt(intake)} is off QuickBooks under the current "
            f"policy (deposit_policy=off_qb). It is excluded from this receipt "
            f"and netted out of the amount treated as tendered, so the receipt "
            f"ties to the bank rather than to the raw batch.")
        receipt.tendered = q(receipt.tendered - intake)


def _add_tips_line(receipt: SalesReceipt, tips: Decimal, config: Config) -> None:
    if q(tips) == ZERO:
        return
    receipt.lines.append(ReceiptLine(
        qb_item=taxonomy.TIPS_PAYABLE,
        description="Tips collected on behalf of staff",
        amount=q(tips),
        tax_code=taxonomy.TAX_E,
    ))


def _annotate(receipt: SalesReceipt, config: Config) -> None:
    if receipt.group == CASH and config.cash_to_undeposited:
        receipt.notes.append(
            f"Cash stays in {config.accounts.undeposited_funds}. Do not make a "
            f"QuickBooks deposit for it now -- record the deposit on the day "
            f"the cash actually reaches the bank, using the bank's date.")
    if receipt.group == CARD:
        receipt.notes.append(
            f"Deposit this receipt from {config.accounts.undeposited_funds} to "
            f"{config.accounts.bank} as one deposit matching the closeout "
            f"batch, so it clears against the processor's settlement.")
    if receipt.group == DEPOSIT_REDEEMED:
        if config.deposit_policy == DEPOSITS_LIABILITY:
            receipt.notes.append(
                f"No money arrives today. This receipt draws down "
                f"{config.accounts.customer_deposits} against revenue now "
                f"earned.")
        else:
            receipt.notes.append(
                f"No money arrives today, and under deposit_policy=off_qb the "
                f"intake side was never recorded. Posting this as-is will "
                f"drive {config.accounts.customer_deposits} negative by the "
                f"same amount. Confirm the intended treatment before posting.")
    if receipt.group == GIFTCARD:
        receipt.notes.append(
            f"No money arrives today. This receipt draws down "
            f"{config.accounts.gift_certificates}, which was credited when the "
            f"certificate was sold.")


def _deposit_to(group: str, config: Config) -> str:
    if group == DEPOSIT_REDEEMED:
        return config.accounts.customer_deposits
    if group == GIFTCARD:
        return config.accounts.gift_certificates
    return config.accounts.undeposited_funds


def _prefix(config: Config) -> str:
    letters = [w[0] for w in config.qb_class.split() if w]
    return ("".join(letters) or "SR").upper()[:4]
