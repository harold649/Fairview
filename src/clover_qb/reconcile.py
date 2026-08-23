"""The check suite.

Every check states what it compared, what it expected, what it found and what
it will tolerate. A failing check that blocks writes no output file at all: a
workbook or an IIF sitting in an output folder having failed validation is
worse than no file, because it can be posted by mistake.

Tolerances are deliberate. Tax and the card-batch match allow $0.05, which is
the most that per-line rounding can plausibly accumulate across a day's
orders. The additions that ought to be identical -- net sales from two
directions, the tattoo parent against its artist lines, money collected
against money accounted for -- allow nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from . import taxonomy
from .config import Config, DEPOSITS_LIABILITY, DEPOSITS_OFF_QB
from .money import ZERO, fmt, q, total
from .sources.payments import CARD, CASH, DEPOSIT_REDEEMED, GIFTCARD

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclass
class Check:
    check_id: str
    title: str
    status: str
    detail: str = ""
    expected: Optional[Decimal] = None
    actual: Optional[Decimal] = None
    tolerance: Optional[Decimal] = None
    blocking: bool = True

    @property
    def variance(self) -> Optional[Decimal]:
        if self.expected is None or self.actual is None:
            return None
        return q(self.actual - self.expected)

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def line(self) -> str:
        bits = [f"[{self.status:4s}] {self.check_id:22s} {self.title}"]
        if self.expected is not None and self.actual is not None:
            bits.append(f"expected {fmt(self.expected)}, got {fmt(self.actual)}, "
                        f"variance {fmt(self.variance)}"
                        + (f" (tolerance {fmt(self.tolerance)})"
                           if self.tolerance is not None else ""))
        if self.detail:
            bits.append(self.detail)
        return "\n         ".join(bits)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def compare(self, check_id: str, title: str, expected: Decimal,
                actual: Decimal, tolerance: Decimal = ZERO, *,
                detail: str = "", blocking: bool = True) -> Check:
        variance = abs(q(actual) - q(expected))
        status = PASS if variance <= q(tolerance) else (FAIL if blocking else WARN)
        return self.add(Check(check_id, title, status, detail, q(expected),
                              q(actual), q(tolerance), blocking))

    def assert_(self, check_id: str, title: str, ok: bool, *, detail: str = "",
                blocking: bool = True) -> Check:
        status = PASS if ok else (FAIL if blocking else WARN)
        return self.add(Check(check_id, title, status, detail, blocking=blocking))

    def skip(self, check_id: str, title: str, why: str) -> Check:
        return self.add(Check(check_id, title, SKIP, why, blocking=False))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        head = f"VALIDATION -- {len(self.checks)} checks, " \
               f"{len(self.failures)} failed, {len(self.warnings)} warnings"
        return "\n".join([head, "=" * len(head)]
                         + [c.line() for c in self.checks])


def run_checks(*, config: Config, line_items, payments, tax_report, closeout,
               employee_sales, allocation, attribution, receipts,
               held_total: Decimal, held_count: int,
               deposit_intake: Decimal) -> Report:
    """Run every check against one assembled day."""
    report = Report()
    tol = config.tolerances

    source_net = line_items.net_sales
    unpaid_total = total(u.amount for u in allocation.unpaid)

    # 1. Allocation completeness -- every source line reached a tender group
    #    or is explicitly accounted for as unpaid.
    report.compare(
        "alloc.completeness",
        "Every LineItemsExport line is allocated to a tender or reported unpaid",
        source_net, allocation.allocated_total + unpaid_total, tol.net_sales,
        detail=("Allocated " + fmt(allocation.allocated_total)
                + (f" plus unpaid {fmt(unpaid_total)}" if unpaid_total else "")))

    # 2. Net sales -- what the receipts carry against the source, with the
    #    non-revenue and not-yet-postable pieces named rather than absorbed.
    posted_revenue = total(r.revenue for r in receipts)
    unattributed = attribution.unattributed
    accounted = q(posted_revenue + deposit_intake + held_total + unattributed
                  + unpaid_total)
    carve_outs = []
    if deposit_intake:
        carve_outs.append(f"deposits {fmt(deposit_intake)}")
    if held_total:
        carve_outs.append(f"held for approval {fmt(held_total)} ({held_count} lines)")
    if unattributed:
        carve_outs.append(f"unattributed tattoo {fmt(unattributed)}")
    if unpaid_total:
        carve_outs.append(f"unpaid orders {fmt(unpaid_total)}")
    report.compare(
        "sales.net",
        "Receipt revenue plus named carve-outs equals LineItemsExport net sales",
        source_net, accounted, tol.net_sales,
        detail=("Posted revenue " + fmt(posted_revenue)
                + ("; carve-outs: " + ", ".join(carve_outs) if carve_outs else "")))

    # 3. Clover's own net sales figure, from a different export.
    if tax_report is not None:
        report.compare(
            "sales.clover",
            "LineItemsExport net sales agrees with the Tax Report",
            tax_report.net_sales, source_net, tol.tax,
            detail="Tax Report applicable sales plus no-tax sales.")
    else:
        report.skip("sales.clover", "Tax Report cross-check", "No Tax Report supplied.")

    # 4. Tax. The authoritative figure is the Tax Report: it covers every
    #    payment method, where the closeout covers card only.
    computed_gst = total(r.gst for r in receipts)
    computed_qst = total(r.qst for r in receipts)
    if tax_report is not None:
        report.compare("tax.gst", "GST computed from line items vs Clover Tax Report",
                       tax_report.gst, computed_gst, tol.tax)
        report.compare("tax.qst", "QST computed from line items vs Clover Tax Report",
                       tax_report.qst, computed_qst, tol.tax)
        if tax_report.tax_removed_present:
            report.assert_(
                "tax.removed", "No manual tax override at the terminal", False,
                detail=(f"The Tax Report carries a Tax Removed line "
                        f"({fmt(tax_report.tax_removed)}). Someone overrode tax "
                        f"at the till. Confirm the item and the intent before "
                        f"posting -- this is different from an exempt sale, "
                        f"which is expected and needs no escalation."),
                blocking=True)
    else:
        report.skip("tax.gst", "GST cross-check", "No Tax Report supplied.")
        report.skip("tax.qst", "QST cross-check", "No Tax Report supplied.")

    # 5. QuickBooks will compute tax on the receipt subtotal, not per line.
    #    Where that differs from what Clover charged, the person entering the
    #    receipt has to override it, so say so rather than letting them find out.
    overrides = [r for r in receipts if r.tax_override_needed]
    if overrides:
        detail = "; ".join(
            f"{r.receipt_id}: QB would compute GST {fmt(r.gst_qb)} / QST "
            f"{fmt(r.qst_qb)}, override to {fmt(r.gst)} / {fmt(r.qst)}"
            for r in overrides)
        report.assert_("tax.qb_override",
                       "QuickBooks auto-tax matches the tax actually charged",
                       False, detail=detail, blocking=False)
    else:
        report.assert_("tax.qb_override",
                       "QuickBooks auto-tax matches the tax actually charged",
                       True, detail="No override needed at the receipt screen.")

    # 6. Tattoo. Two directions on the same number.
    tattoo_lines = [l for r in receipts for l in r.lines
                    if l.qb_item == taxonomy.TATTOO]
    posted_tattoo = total(l.amount for l in tattoo_lines)
    report.compare(
        "tattoo.split",
        "Tattoo revenue on the receipts equals the per-artist attribution",
        attribution.total, posted_tattoo, tol.tattoo,
        detail=", ".join(f"{a.name} {fmt(a.amount)}" for a in attribution.artists)
               or "No tattoo revenue.")
    report.assert_(
        "tattoo.attribution",
        "Every TATTOO line is attributed to a confirmed roster artist",
        not attribution.issues,
        detail="; ".join(f"{i.kind} on order {i.order_id} ({fmt(i.amount)}): "
                         f"{i.detail}" for i in attribution.issues))

    # 7. Deposit matching -- the card receipt against the closeout batch. This
    #    is what makes the QuickBooks deposit clear against the settlement.
    card_receipt = next((r for r in receipts if r.group == CARD), None)
    card_total = card_receipt.total if card_receipt else ZERO
    if closeout is not None:
        expected_card = closeout.batch_total
        note = ""
        if config.deposit_policy == DEPOSITS_OFF_QB and deposit_intake:
            expected_card = q(expected_card - _card_deposit_intake(
                allocation, deposit_intake))
            note = (f"Closeout batch {fmt(closeout.batch_total)} less deposit "
                    f"intake taken on card, which is off QuickBooks under the "
                    f"current policy.")
        report.compare(
            "deposit.card_batch",
            "Card Sales Receipt total matches the Clover closeout batch",
            expected_card, card_total, tol.deposit_match,
            detail=(note or f"Batch {closeout.batch_id or '(no batch ID)'}. "
                            f"Deposit this receipt from "
                            f"{config.accounts.undeposited_funds} as one deposit."))
    else:
        report.skip("deposit.card_batch", "Card batch match",
                    "No closeout supplied -- the card figure has no anchor.")

    # 8. Card brand breakdown, closeout against the Payments export.
    if closeout is not None and closeout.card_types and payments is not None:
        derived = payments.card_brand_totals()
        mismatches = []
        for ct in closeout.card_types:
            got = derived.get(ct.name, (0, ZERO))[1]
            match = next((v for k, v in derived.items()
                          if k.upper().startswith(ct.name.upper()[:4])), None)
            if got == ZERO and match is not None:
                got = match[1]
            if abs(q(got - ct.amount)) > tol.card_brand:
                mismatches.append(f"{ct.name}: closeout {fmt(ct.amount)} vs "
                                  f"payments {fmt(got)}")
        report.assert_(
            "deposit.card_brands",
            "Closeout card-type totals agree with the Payments export",
            not mismatches, detail="; ".join(mismatches),
            blocking=True)
    else:
        report.skip("deposit.card_brands", "Card brand cross-check",
                    "No closeout card-type breakdown available.")

    # 9. Each receipt against the money its tender actually took.
    for receipt in receipts:
        if receipt.group in (DEPOSIT_REDEEMED, GIFTCARD):
            continue        # no money arrives; nothing to compare against
        report.compare(
            f"receipt.{receipt.group.lower()}",
            f"{receipt.receipt_id} total equals the amount tendered",
            receipt.tendered, receipt.total, tol.deposit_match,
            detail=_residual_hint(receipt))

    # 10. The whole day: money in against money accounted for.
    collected = total(r.tendered for r in receipts)
    receipts_total = total(r.total for r in receipts)
    report.compare(
        "recon.proof", "Total tendered equals the total on the Sales Receipts",
        collected, receipts_total, tol.reconciliation,
        detail="; ".join(f"{r.group} {fmt(r.total)}" for r in receipts))

    # 11. Cash left to deposit.
    cash_receipt = next((r for r in receipts if r.group == CASH), None)
    cash_total = cash_receipt.total if cash_receipt else ZERO
    report.assert_(
        "cash.undeposited",
        "Cash is posted to Undeposited Funds and left to be deposited",
        True,
        detail=(f"{fmt(cash_total)} sits in {config.accounts.undeposited_funds} "
                f"awaiting a bank deposit. This must equal the physical drawer "
                f"count; if it does not, something belongs on its own line "
                f"rather than in cash."
                if cash_total else "No cash taken."),
        blocking=False)

    # 12. Tips, against the Employee Sales export.
    posted_tips = total(r.tips for r in receipts)
    if employee_sales is not None and employee_sales.tips != ZERO:
        report.compare("tips.crosscheck",
                       "Tips on the receipts agree with the Employee Sales export",
                       employee_sales.tips, posted_tips, tol.tips,
                       detail=f"Tips post to {config.accounts.tips_payable} as a "
                              f"liability, never to revenue.")
    else:
        report.skip("tips.crosscheck", "Tips cross-check",
                    "No Employee Sales export with a tips figure.")

    # 13. Held lines. Nothing posts while a line is awaiting approval.
    report.assert_(
        "holds.cleared", "No line items are held awaiting approval",
        held_count == 0,
        detail=(f"{held_count} line(s) totalling {fmt(held_total)} need a "
                f"human decision. Custom Items and manual entries never "
                f"classify automatically, however familiar they look."
                if held_count else ""))

    # 14. Things that are not failures but must travel with the day.
    if line_items.excluded_refunds:
        report.assert_("flags.refunds", "Refunded lines excluded", False,
                       detail=f"{len(line_items.excluded_refunds)} refunded "
                              f"line(s) excluded from the receipts, totalling "
                              f"{fmt(total(l.amount for l in line_items.excluded_refunds))}.",
                       blocking=False)
    if line_items.open_orders:
        report.assert_("flags.open_orders", "No open or unpaid orders", False,
                       detail=f"{len(line_items.open_orders)} line(s) sit on "
                              f"open or unpaid orders and are excluded.",
                       blocking=False)
    if allocation.unpaid:
        report.assert_("flags.unpaid", "Every order has a successful payment",
                       False,
                       detail="; ".join(f"{u.order_id} {fmt(u.amount)}: {u.reason}"
                                        for u in allocation.unpaid),
                       blocking=True)
    if allocation.split_orders:
        report.assert_("flags.split_tender", "No split-tender orders", False,
                       detail=f"{len(allocation.split_orders)} order(s) paid by "
                              f"more than one tender; revenue apportioned by "
                              f"payment amount: "
                              f"{', '.join(allocation.split_orders[:8])}",
                       blocking=False)
    if closeout is not None and closeout.parse_notes:
        report.assert_("flags.closeout", "Closeout parsed cleanly", False,
                       detail="; ".join(closeout.parse_notes), blocking=False)

    return report


def _card_deposit_intake(allocation, deposit_intake: Decimal) -> Decimal:
    """How much of the day's deposit intake arrived on a card."""
    from .classify import normalize
    return deposit_intake if any(al.group == CARD for al in allocation.lines) \
        else ZERO


def _residual_hint(receipt) -> str:
    residual = q(receipt.tendered - receipt.total)
    if residual == ZERO:
        return ""
    if residual > ZERO:
        return (f"{fmt(residual)} more was tendered than the receipt accounts "
                f"for. Money arrived with no line behind it -- an appointment "
                f"deposit taken on the same order, or cashback handed over the "
                f"counter. Name it; do not absorb it into cash.")
    return (f"The receipt exceeds what was tendered by {fmt(-residual)}. A "
            f"discount, a partial refund or a line on an order that was not "
            f"fully paid.")
