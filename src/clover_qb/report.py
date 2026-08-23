"""The posting guide -- what a person reads before touching QuickBooks.

Plain text on purpose: it goes in a chat message, an email, or a printout next
to the keyboard, and it has to survive all three. Every figure in it comes
from the same objects the IIF is built from, so the guide and the file cannot
drift apart.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from . import taxonomy
from .config import DEPOSITS_LIABILITY
from .money import ZERO, fmt, q, total
from .sources.payments import CARD, CASH, DEPOSIT_REDEEMED, GIFTCARD

if TYPE_CHECKING:                                    # pragma: no cover
    from .pipeline import Result

WIDTH = 78


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _head(text: str) -> str:
    return f"\n{text}\n{_rule()}"


def render_posting_guide(result: "Result") -> str:
    cfg = result.config
    out: list[str] = []
    out.append(_rule("="))
    out.append(f"{cfg.location} -- QuickBooks Desktop posting guide")
    out.append(f"Business date {result.business_date}   Entity {cfg.entity}   "
               f"Class {cfg.qb_class}")
    out.append(f"Run {result.trail.run_id}")
    out.append(_rule("="))

    if not result.ok:
        out.append("")
        out.append("*** VALIDATION FAILED -- DO NOT POST ***")
        for check in result.report.failures:
            out.append(f"  - {check.check_id}: {check.title}")

    # -- receipts ------------------------------------------------------------
    out.append(_head("SALES RECEIPTS"))
    out.append("One receipt per tender. Enter each as Customers > Enter Sales "
               "Receipts.")
    for receipt in result.receipts:
        out.append("")
        out.append(f"  {receipt.receipt_id}   [{receipt.payment_method}]")
        out.append(f"    Customer   {receipt.customer}")
        out.append(f"    Class      {receipt.qb_class}")
        out.append(f"    Date       {receipt.date}")
        out.append(f"    Deposit to {receipt.deposit_to}")
        out.append(f"    Memo       {receipt.memo}")
        out.append("")
        out.append(f"      {'Item':<34}{'Description':<24}{'Tax':>4}{'Amount':>14}")
        for line in receipt.lines:
            out.append(f"      {line.qb_item:<34}{(line.description or '')[:23]:<24}"
                       f"{line.tax_code:>4}{fmt(line.amount):>14}")
        out.append(f"      {'':<62}{_rule2()}")
        out.append(f"      {'Subtotal':<62}{fmt(receipt.subtotal):>14}")
        out.append(f"      {'GST 5% (as charged)':<62}{fmt(receipt.gst):>14}")
        out.append(f"      {'QST 9.975% (as charged)':<62}{fmt(receipt.qst):>14}")
        out.append(f"      {'TOTAL':<62}{fmt(receipt.total):>14}")
        if receipt.tendered != receipt.total:
            out.append(f"      {'Tendered per Payments export':<62}"
                       f"{fmt(receipt.tendered):>14}")
            out.append(f"      {'Residual -- explain before posting':<62}"
                       f"{fmt(receipt.residual):>14}")
        if receipt.tax_override_needed:
            out.append(f"      Note: QuickBooks will compute GST "
                       f"{fmt(receipt.gst_qb)} / QST {fmt(receipt.qst_qb)} from "
                       f"the subtotal.")
            out.append(f"            Override to {fmt(receipt.gst)} / "
                       f"{fmt(receipt.qst)} to match what was charged.")
        for note in receipt.notes:
            out.append(f"      Note: {note}")

    # -- deposits ------------------------------------------------------------
    out.append(_head("BANKING"))
    card = result.receipt(CARD)
    cash = result.receipt(CASH)
    close = result.sources.get("closeout")
    if card:
        out.append(f"  Card    {fmt(card.total)} in {cfg.accounts.undeposited_funds}.")
        if close is not None:
            out.append(f"          Closeout batch {close.batch_id or '(no batch ID)'} "
                       f"{fmt(close.batch_total)}.")
            variance = q(card.total - close.batch_total)
            out.append(f"          Variance {fmt(variance)}.")
        out.append(f"          Make one deposit to {cfg.accounts.bank} for this "
                   f"amount so it clears")
        out.append(f"          against the processor's settlement.")
    if cash:
        out.append("")
        out.append(f"  Cash    {fmt(cash.total)} in {cfg.accounts.undeposited_funds} "
                   f"-- LEFT TO BE DEPOSITED.")
        out.append(f"          Do not create a deposit now. Record it on the day "
                   f"the cash reaches")
        out.append(f"          the bank, using the bank's date. This figure must "
                   f"equal the drawer.")
    for receipt in result.receipts:
        if receipt.group in (GIFTCARD, DEPOSIT_REDEEMED):
            out.append("")
            out.append(f"  {receipt.group:<8}{fmt(receipt.total)} -- no money "
                       f"arrives. Draws down {receipt.deposit_to}.")

    # -- liabilities ---------------------------------------------------------
    tips = total(r.tips for r in result.receipts)
    out.append(_head("LIABILITIES"))
    out.append(f"  Tips              {fmt(tips):>14}  to {cfg.accounts.tips_payable}")
    out.append(f"                    {'':>14}  Never revenue. Clears when staff "
               f"are paid out.")
    intake = result.trail.figures.get("deposit_intake", ZERO)
    if intake:
        treatment = ("posted to " + cfg.accounts.customer_deposits
                     if cfg.deposit_policy == DEPOSITS_LIABILITY
                     else "OFF QuickBooks (deposit_policy=off_qb)")
        out.append(f"  Deposits taken    {fmt(intake):>14}  {treatment}")

    # -- tattoo --------------------------------------------------------------
    if result.attribution.artists or result.attribution.issues:
        out.append(_head("TATTOO BY ARTIST"))
        for artist in result.attribution.artists:
            out.append(f"  {artist.name:<24}{fmt(artist.amount):>14}   "
                       f"{artist.line_count} line(s), "
                       f"{len(artist.order_ids)} order(s)")
        out.append(f"  {'TOTAL':<24}{fmt(result.attribution.total):>14}")
        for issue in result.attribution.issues:
            out.append(f"  ! {issue.kind} on order {issue.order_id} "
                       f"({fmt(issue.amount)}): {issue.detail}")

    # -- tax -----------------------------------------------------------------
    tax = result.sources.get("tax_report")
    out.append(_head("TAX"))
    gst = total(r.gst for r in result.receipts)
    qst = total(r.qst for r in result.receipts)
    out.append(f"  {'':<24}{'Computed':>14}{'Clover':>14}{'Variance':>14}")
    if tax is not None:
        out.append(f"  {'GST 5%':<24}{fmt(gst):>14}{fmt(tax.gst):>14}"
                   f"{fmt(q(gst - tax.gst)):>14}")
        out.append(f"  {'QST 9.975%':<24}{fmt(qst):>14}{fmt(tax.qst):>14}"
                   f"{fmt(q(qst - tax.qst)):>14}")
        out.append(f"  Tolerance {fmt(cfg.tolerances.tax)}. The Tax Report is "
                   f"authoritative: it covers every")
        out.append(f"  payment method, where the closeout covers card only.")
    else:
        out.append(f"  {'GST 5%':<24}{fmt(gst):>14}{'n/a':>14}{'n/a':>14}")
        out.append(f"  {'QST 9.975%':<24}{fmt(qst):>14}{'n/a':>14}{'n/a':>14}")
        out.append("  No Tax Report supplied -- the tax figures are unverified.")

    # -- exceptions ----------------------------------------------------------
    blocking = [e for e in result.trail.exceptions if e.blocking]
    advisory = [e for e in result.trail.exceptions if not e.blocking]
    out.append(_head("EXCEPTIONS"))
    if not result.trail.exceptions:
        out.append("  None.")
    for group, rows in (("BLOCKING", blocking), ("FOR THE RECORD", advisory)):
        if not rows:
            continue
        out.append(f"  {group}")
        for row in rows:
            out.append(f"    EX-{row.ex_num} [{row.kind}] {row.reference} "
                       f"{fmt(row.amount) if row.amount else ''}")
            out.append(f"          {row.description}")
            out.append(f"          Action: {row.action}")

    # -- validation ----------------------------------------------------------
    out.append(_head("VALIDATION"))
    out.append(result.report.render())
    out.append("")
    return "\n".join(out) + "\n"


def _rule2() -> str:
    return "-" * 14
