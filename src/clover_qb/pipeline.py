"""End-to-end: a day of Clover exports in, QuickBooks Sales Receipts out.

The order is fixed and each step depends on the last:

    gate and parse -> classify -> attribute -> allocate to tenders ->
    build receipts -> validate -> emit

Nothing is written until validation passes. A blocking failure produces the
report and no files, because an output that failed its checks is worse than no
output: it can be posted by mistake and nothing downstream will catch it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from . import attribution as attribution_mod
from . import audit as audit_mod
from . import iif as iif_mod
from . import taxonomy
from .classify import Classifier, Decision
from .config import Config, DEPOSITS_LIABILITY, DEPOSITS_OFF_QB
from .dictionary import Dictionary, normalize
from .money import ZERO, fmt, q, total
from .receipts import SalesReceipt, build_receipts
from .reconcile import Report, run_checks
from .sources import closeout as closeout_mod
from .sources import employee_sales as employee_sales_mod
from .sources import line_items as line_items_mod
from .sources import payments as payments_mod
from .sources import tax_report as tax_report_mod
from .sources.payments import CARD, CASH
from .tenders import Allocation, allocate_orders


@dataclass
class Inputs:
    line_items: str
    payments: str
    tax_report: Optional[str] = None
    closeout: Optional[str] = None
    employee_sales: Optional[str] = None

    def present(self) -> list[tuple[str, str]]:
        return [(role, path) for role, path in
                (("line_items", self.line_items), ("payments", self.payments),
                 ("tax_report", self.tax_report), ("closeout", self.closeout),
                 ("employee_sales", self.employee_sales)) if path]


@dataclass
class Result:
    business_date: str
    config: Config
    receipts: list[SalesReceipt]
    report: Report
    trail: audit_mod.AuditTrail
    allocation: Allocation
    attribution: attribution_mod.Attribution
    decisions: dict[str, Decision]
    held: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.report.ok

    def receipt(self, group: str) -> Optional[SalesReceipt]:
        return next((r for r in self.receipts if r.group == group), None)

    @property
    def cash_to_deposit(self) -> Decimal:
        receipt = self.receipt(CASH)
        return receipt.total if receipt else ZERO

    @property
    def card_to_deposit(self) -> Decimal:
        receipt = self.receipt(CARD)
        return receipt.total if receipt else ZERO


def run(inputs: Inputs, config: Config, *, business_date: str,
        approvals: Optional[dict[str, str]] = None) -> Result:
    """Process one day. Never writes anything; see :func:`emit`."""
    trail = audit_mod.AuditTrail.start(business_date, config.to_dict())

    # -- parse ----------------------------------------------------------------
    li = line_items_mod.load(inputs.line_items)
    pay = payments_mod.load(inputs.payments)
    tax = tax_report_mod.load(inputs.tax_report) if inputs.tax_report else None
    close = closeout_mod.load(inputs.closeout) if inputs.closeout else None
    emp = (employee_sales_mod.load(inputs.employee_sales)
           if inputs.employee_sales else None)

    rows = {"line_items": len(li.lines) + len(li.excluded_refunds) + len(li.open_orders),
            "payments": len(pay.payments) + len(pay.rejected)}
    for role, path in inputs.present():
        trail.inputs.append(audit_mod.InputFile.of(role, path, rows.get(role)))

    # -- classify -------------------------------------------------------------
    dictionary = (Dictionary.load(config.dictionary_path)
                  if config.dictionary_path else Dictionary.empty())
    classifier = Classifier(
        dictionary, approvals,
        escalate_defaults=config.escalate_default_classifications)

    decisions: dict[str, Decision] = {}
    seen: dict[str, dict] = {}
    for line in li.lines:
        key = normalize(line.item_name)
        if key not in decisions:
            decisions[key] = classifier.classify(line.item_name,
                                                 item_id=line.item_id)
            seen[key] = {"item_id": line.item_id, "count": 0, "amount": ZERO}
        seen[key]["count"] += 1
        seen[key]["amount"] += line.amount

    for key, decision in decisions.items():
        info = seen[key]
        trail.decisions.append(audit_mod.DecisionRow(
            item_name=decision.item_name, item_id=info["item_id"],
            occurrences=info["count"], amount=q(info["amount"]),
            qb_item=decision.qb_item or "", proposal=decision.proposal or "",
            rule_id=decision.rule_id, rule_priority=decision.priority,
            rationale=decision.rationale, escalate=decision.escalate,
            hold=decision.hold))

    for entry in dictionary.unresolvable():
        trail.add_exception(
            "dictionary", entry.item_name, ZERO,
            f"Locked dictionary maps {entry.item_name!r} to {entry.qb_item!r}, "
            f"which is not in the unified taxonomy (line {entry.line_no}).",
            "Correct the dictionary entry; until then the item falls through "
            "to a keyword rule as if the dictionary had never been consulted.",
            blocking=False)

    # -- attribute ------------------------------------------------------------
    attribution = attribution_mod.attribute(li.lines, config)
    for issue in attribution.issues:
        trail.add_exception(
            issue.kind, issue.order_id, issue.amount,
            f"{issue.item_name} under {issue.employee or '(no employee)'}: "
            f"{issue.detail}",
            "Confirm the roster or resolve the disagreement at the shop, then "
            "rerun. The amount is deliberately left off the receipts until "
            "then, so it shows as a variance rather than landing on the wrong "
            "artist.",
            blocking=True)

    # -- allocate -------------------------------------------------------------
    allocation = allocate_orders(li.lines, pay.by_order())

    held_lines = [al for al in allocation.lines
                  if not (decisions.get(normalize(al.line.item_name)) or
                          Decision("", None, "S", "missing", 99, "")).postable]
    held_total = total(al.amount for al in held_lines)
    held_keys = {normalize(al.line.item_name) for al in held_lines}
    for key in sorted(held_keys):
        decision = decisions[key]
        amount = total(al.amount for al in held_lines
                       if normalize(al.line.item_name) == key)
        trail.add_exception(
            "held_item", decision.item_name, amount,
            f"{decision.rationale}"
            + (f" Proposed: {decision.proposal}." if decision.proposal else ""),
            "Approve with --approve \"NAME=QB Item\" (or add the item to the "
            "locked dictionary) and rerun. Custom Items and manual entries "
            "never classify automatically, however familiar they look.",
            blocking=True)

    # -- deposits -------------------------------------------------------------
    deposit_by_group: dict[str, Decimal] = {}
    for al in allocation.lines:
        decision = decisions.get(normalize(al.line.item_name))
        if decision and decision.qb_item == taxonomy.CUSTOMER_DEPOSITS:
            deposit_by_group[al.group] = deposit_by_group.get(al.group, ZERO) + al.amount
    deposit_intake = total(deposit_by_group.values())
    if deposit_intake:
        trail.add_exception(
            "deposit_intake", business_date, deposit_intake,
            f"Appointment deposits of {fmt(deposit_intake)} taken today.",
            _deposit_action(config), blocking=False)

    # -- build ----------------------------------------------------------------
    # Deposits that appear as line items are found by build_receipts itself,
    # from the same allocated lines; deposit_intake_by_group is only for intake
    # with no line behind it, which this pipeline does not currently produce.
    receipts = build_receipts(
        allocation, decisions, attribution, config,
        date_iso=business_date,
        tendered_by_group=pay.group_totals(),
        deposit_intake_by_group={},
    )

    # -- validate -------------------------------------------------------------
    report = run_checks(
        config=config, line_items=li, payments=pay, tax_report=tax,
        closeout=close, employee_sales=emp, allocation=allocation,
        attribution=attribution, receipts=receipts,
        held_total=held_total, held_count=len(held_lines),
        # Always the true intake, whatever the policy: a deposit is never
        # counted in receipt.revenue (it posts as a liability line or not at
        # all), so the net-sales check needs the real figure as a carve-out
        # either way. The card-batch check (reconcile.run_checks) applies its
        # own off_qb-only netting internally.
        deposit_intake=deposit_intake)

    _trace(trail, li, allocation, decisions, attribution, receipts, config)
    _figures(trail, li, pay, tax, close, receipts, allocation, attribution,
             config, deposit_intake, held_total, len(held_lines))
    trail.checks = [{
        "id": c.check_id, "title": c.title, "status": c.status,
        "expected": c.expected, "actual": c.actual, "variance": c.variance,
        "tolerance": c.tolerance, "blocking": c.blocking, "detail": c.detail,
    } for c in report.checks]

    for check in report.checks:
        if check.status in ("FAIL", "WARN") and check.check_id.startswith("flags."):
            trail.add_exception("flag", check.check_id, ZERO, check.detail,
                                "Acknowledge; travels with the day's record.",
                                blocking=False)

    return Result(business_date, config, receipts, report, trail, allocation,
                  attribution, decisions, held_lines,
                  {"line_items": li, "payments": pay, "tax_report": tax,
                   "closeout": close, "employee_sales": emp})


def emit(result: Result, out_dir: str | Path, *, force: bool = False,
         tax_lines: bool = True, workbook: bool = True) -> list[Path]:
    """Write the outputs, but only if validation passed.

    ``force`` overrides that and marks every file it writes as unvalidated, in
    the filename and in the manifest. It exists for diagnosis, not posting.
    """
    if not result.ok and not force:
        raise ValidationFailed(result.report)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = result.business_date.replace("-", "")
    prefix = f"{result.config.qb_class}_{stamp}"
    if not result.ok:
        prefix = f"UNVALIDATED_{prefix}"

    written: list[Path] = []
    written.append(iif_mod.write(out / f"{prefix}_SalesReceipts.iif",
                                 result.receipts, result.config,
                                 tax_lines=tax_lines))
    written.append(_write_summary(out / f"{prefix}_PostingGuide.txt", result))
    if workbook:
        try:
            from .workbook import write_workbook
            written.append(write_workbook(out / f"{prefix}_QB.xlsx", result))
        except ImportError:
            (out / "NOTE_no_workbook.txt").write_text(
                "openpyxl is not installed, so no Excel workbook was written. "
                "The IIF, the posting guide and the audit trail are complete "
                "without it. Install with: pip install 'clover-qb[xlsx]'\n",
                encoding="utf-8")

    result.trail.outputs = [str(p) for p in written]
    written.extend(result.trail.write(out / "audit"))
    return written


class ValidationFailed(Exception):
    def __init__(self, report: Report):
        self.report = report
        super().__init__(
            f"{len(report.failures)} check(s) failed; no files written. "
            + "; ".join(c.check_id for c in report.failures))


# --- internals -------------------------------------------------------------

def _deposit_action(config: Config) -> str:
    if config.deposit_policy == DEPOSITS_LIABILITY:
        return (f"Posted to {config.accounts.customer_deposits} as a liability "
                f"line on the receipt. The receipt total stays equal to the "
                f"raw batch, so the deposit still matches the settlement.")
    return ("Kept off QuickBooks under deposit_policy=off_qb, and netted out "
            "of the card figure so the receipt ties to the bank. The two "
            "treatments both reconcile, so no check will catch the wrong one "
            "being chosen -- confirm the policy is the intended one.")


def _trace(trail, li, allocation, decisions, attribution, receipts, config):
    artist_by_line = {}
    for a in attribution.artists:
        for line in attribution.lines:
            if config.canonical_artist(line.employee) == a.name:
                artist_by_line[(line.order_id, line.item_name, line.row_no)] = a.name
    issue_lines = {(i.order_id, i.item_name) for i in attribution.issues}
    receipt_by_group = {r.group: r.receipt_id for r in receipts}

    for al in allocation.lines:
        line = al.line
        decision = decisions.get(normalize(line.item_name))
        artist = artist_by_line.get((line.order_id, line.item_name, line.row_no), "")
        if decision is None:
            status, qb_item, account = "held", "", ""
        elif decision.hold:
            status, qb_item, account = "held", "", ""
        elif decision.qb_item == taxonomy.TATTOO and (line.order_id, line.item_name) in issue_lines:
            status, qb_item, account = "unattributed", decision.qb_item, ""
        else:
            status = "posted"
            qb_item = decision.qb_item
            account = taxonomy.resolve(qb_item).account
        tax = taxonomy.compute_tax(al.amount, decision.tax_code if decision else "E") \
            if status == "posted" else taxonomy.TaxSplit(al.amount, ZERO, ZERO)

        trail.traces.append(audit_mod.TraceRow(
            source_row=line.row_no, order_id=line.order_id,
            order_date=line.order_date, employee=line.employee,
            item_name=line.item_name, item_id=line.item_id,
            source_amount=line.amount, allocated_amount=al.amount,
            allocation_share=f"{al.share:.4f}" if al.split else "1.0000",
            split_tender=al.split, tender_group=al.group,
            payment_ids=";".join(al.payment_ids),
            rule_id=decision.rule_id if decision else "unclassified",
            rule_priority=decision.priority if decision else 99,
            rule_rationale=decision.rationale if decision else "",
            qb_item=qb_item, qb_account=account,
            tax_code=decision.tax_code if decision else "",
            gst=tax.gst, qst=tax.qst, artist=artist,
            receipt_id=receipt_by_group.get(al.group, "") if status == "posted" else "",
            status=status,
            note="Split across tenders" if al.split else ""))

    for group, rows, status, note in (
            ("", li.excluded_refunds, "refunded", "Refunded line, excluded"),
            ("", li.open_orders, "open", "Open or unpaid order, excluded")):
        for line in rows:
            trail.traces.append(audit_mod.TraceRow(
                source_row=line.row_no, order_id=line.order_id,
                order_date=line.order_date, employee=line.employee,
                item_name=line.item_name, item_id=line.item_id,
                source_amount=line.amount, allocated_amount=ZERO,
                allocation_share="0.0000", split_tender=False, tender_group="",
                payment_ids="", rule_id="excluded", rule_priority=0,
                rule_rationale=note, qb_item="", qb_account="", tax_code="",
                gst=ZERO, qst=ZERO, artist="", receipt_id="", status=status,
                note=note))

    for unpaid in allocation.unpaid:
        trail.add_exception("unpaid_order", unpaid.order_id, unpaid.amount,
                            unpaid.reason,
                            "Confirm how the order was settled. Do not fold it "
                            "into cash.", blocking=True)


def _figures(trail, li, pay, tax, close, receipts, allocation, attribution,
             config, deposit_intake, held_total, held_count):
    trail.figures = {
        "net_sales_line_items": li.net_sales,
        "net_sales_tax_report": tax.net_sales if tax else None,
        "gst_computed": total(r.gst for r in receipts),
        "qst_computed": total(r.qst for r in receipts),
        "gst_tax_report": tax.gst if tax else None,
        "qst_tax_report": tax.qst if tax else None,
        "tips": total(r.tips for r in receipts),
        "tattoo_total": attribution.total,
        "tattoo_by_artist": {a.name: a.amount for a in attribution.artists},
        "deposit_intake": deposit_intake,
        "held_total": held_total,
        "held_count": held_count,
        "closeout_batch_id": close.batch_id if close else None,
        "closeout_batch_total": close.batch_total if close else None,
        "receipts": [{
            "receipt_id": r.receipt_id, "group": r.group,
            "deposit_to": r.deposit_to, "revenue": r.revenue,
            "gst": r.gst, "qst": r.qst, "tips": r.tips,
            "total": r.total, "tendered": r.tendered, "residual": r.residual,
        } for r in receipts],
        "cash_to_deposit": next((r.total for r in receipts if r.group == CASH), ZERO),
        "card_to_deposit": next((r.total for r in receipts if r.group == CARD), ZERO),
    }


def _write_summary(path: Path, result: Result) -> Path:
    from .report import render_posting_guide
    path.write_text(render_posting_guide(result), encoding="utf-8")
    return path
