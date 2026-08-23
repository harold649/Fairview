"""Excel workbook output.

Optional: everything the pipeline produces is complete without it. The
workbook exists because a reviewer wants to sort and filter the day, not
because any figure lives only here.

Five sheets: the receipts as they are to be entered, the line-by-line trace,
the tattoo split, the exceptions, and the validation results.

Needs openpyxl (``pip install 'clover-qb[xlsx]'``).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import taxonomy
from .money import ZERO, q, total
from .sources.payments import CARD, CASH

if TYPE_CHECKING:                                    # pragma: no cover
    from .pipeline import Result

MONEY = '#,##0.00;-#,##0.00'
TITLE = Font(bold=True, size=14)
HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
SECTION = Font(bold=True)
SECTION_FILL = PatternFill("solid", fgColor="D9E1F2")
FAIL_FILL = PatternFill("solid", fgColor="F8CBAD")
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
PASS_FILL = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)


def write_workbook(path: str | Path, result: "Result") -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    _receipts_sheet(wb, result)
    _detail_sheet(wb, result)
    _tattoo_sheet(wb, result)
    _exceptions_sheet(wb, result)
    _validation_sheet(wb, result)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(p)
    return p


def _headers(ws, row: int, headers: Sequence[str], widths: Sequence[int]) -> int:
    for col, (text, width) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font, cell.fill, cell.border = HEAD, HEAD_FILL, BOX
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = ws.cell(row=row + 1, column=1)
    return row + 1


def _money(ws, row: int, col: int, value: Decimal, *, bold: bool = False):
    cell = ws.cell(row=row, column=col, value=float(q(value)))
    cell.number_format = MONEY
    if bold:
        cell.font = SECTION
    return cell


def _section(ws, row: int, text: str, span: int = 6) -> int:
    cell = ws.cell(row=row, column=1, value=text)
    cell.font, cell.fill = SECTION, SECTION_FILL
    for col in range(2, span + 1):
        ws.cell(row=row, column=col).fill = SECTION_FILL
    return row + 1


def _receipts_sheet(wb, result: "Result") -> None:
    ws = wb.create_sheet("QB Sales Receipts")
    cfg = result.config
    ws["A1"] = f"{cfg.location} -- Sales Receipts for {result.business_date}"
    ws["A1"].font = TITLE
    ws["A2"] = (f"Entity {cfg.entity}   Class {cfg.qb_class}   "
                f"Customer {cfg.customer}   Run {result.trail.run_id}")
    row = 4
    if not result.ok:
        ws.cell(row=row, column=1,
                value="VALIDATION FAILED -- DO NOT POST").font = Font(
            bold=True, color="C00000", size=12)
        row += 2

    row = _headers(ws, row, ["QB Item", "Description", "Tax", "Amount",
                             "GST", "QST", "Lines"],
                   [34, 30, 6, 14, 12, 12, 8])
    for receipt in result.receipts:
        row = _section(ws, row,
                       f"{receipt.receipt_id}  |  {receipt.payment_method}  |  "
                       f"Deposit to {receipt.deposit_to}", span=7)
        for line in receipt.lines:
            ws.cell(row=row, column=1, value=line.qb_item)
            ws.cell(row=row, column=2, value=line.description)
            ws.cell(row=row, column=3, value=line.tax_code)
            _money(ws, row, 4, line.amount)
            _money(ws, row, 5, line.gst)
            _money(ws, row, 6, line.qst)
            ws.cell(row=row, column=7, value=line.line_count)
            row += 1
        for label, value in (("Subtotal", receipt.subtotal),
                             ("GST 5% (as charged)", receipt.gst),
                             ("QST 9.975% (as charged)", receipt.qst),
                             ("RECEIPT TOTAL", receipt.total),
                             ("Tendered per Payments export", receipt.tendered),
                             ("Residual", receipt.residual)):
            ws.cell(row=row, column=2, value=label).font = SECTION
            _money(ws, row, 4, value, bold=label == "RECEIPT TOTAL")
            row += 1
        if receipt.tax_override_needed:
            ws.cell(row=row, column=2,
                    value=(f"QuickBooks will compute GST {q(receipt.gst_qb)} / "
                           f"QST {q(receipt.qst_qb)} from the subtotal. "
                           f"Override to match what was charged."))
            ws.cell(row=row, column=2).fill = WARN_FILL
            row += 1
        for note in receipt.notes:
            ws.cell(row=row, column=2, value=note).alignment = Alignment(wrap_text=False)
            row += 1
        row += 1

    row = _section(ws, row, "BANKING", span=7)
    card = result.receipt(CARD)
    cash = result.receipt(CASH)
    close = result.sources.get("closeout")
    if card:
        ws.cell(row=row, column=1, value="Card to deposit")
        _money(ws, row, 4, card.total, bold=True)
        ws.cell(row=row, column=5,
                value=(f"Closeout {close.batch_id or '(no batch ID)'} "
                       f"{q(close.batch_total)}" if close else
                       "No closeout supplied"))
        row += 1
    if cash:
        ws.cell(row=row, column=1, value="Cash LEFT TO BE DEPOSITED")
        _money(ws, row, 4, cash.total, bold=True)
        ws.cell(row=row, column=5,
                value=(f"Stays in {result.config.accounts.undeposited_funds}. "
                       f"Record the bank deposit on the day it clears."))
        row += 1


def _detail_sheet(wb, result: "Result") -> None:
    ws = wb.create_sheet("POS Detail")
    headers = ["Row", "Order ID", "Employee", "Item Name", "Item ID",
               "Source Amt", "Allocated", "Tender", "Rule", "Pri", "QB Item",
               "Account", "Tax", "GST", "QST", "Artist", "Receipt", "Status",
               "Note"]
    widths = [6, 16, 14, 34, 16, 12, 12, 10, 20, 5, 30, 26, 5, 10, 10, 12, 24,
              13, 30]
    row = _headers(ws, 1, headers, widths)
    for t in result.trail.traces:
        values = [t.source_row, t.order_id, t.employee, t.item_name, t.item_id,
                  None, None, t.tender_group, t.rule_id, t.rule_priority,
                  t.qb_item, t.qb_account, t.tax_code, None, None, t.artist,
                  t.receipt_id, t.status, t.note]
        for col, value in enumerate(values, start=1):
            if value is not None:
                ws.cell(row=row, column=col, value=value)
        _money(ws, row, 6, t.source_amount)
        _money(ws, row, 7, t.allocated_amount)
        _money(ws, row, 14, t.gst)
        _money(ws, row, 15, t.qst)
        if t.status in ("held", "unattributed", "unpaid"):
            for col in range(1, len(headers) + 1):
                ws.cell(row=row, column=col).fill = FAIL_FILL
        row += 1

    ws.cell(row=row + 1, column=4, value="Clover LineItemsExport net sales").font = SECTION
    _money(ws, row + 1, 6, result.sources["line_items"].net_sales, bold=True)
    ws.cell(row=row + 2, column=4, value="Allocated to tenders").font = SECTION
    _money(ws, row + 2, 7, result.allocation.allocated_total, bold=True)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row - 1}"


def _tattoo_sheet(wb, result: "Result") -> None:
    ws = wb.create_sheet("Tattoo by Artist")
    ws["A1"] = "Tattoo attribution"
    ws["A1"].font = TITLE
    ws["A2"] = ("Order Employee Name is the attribution key. A per-artist SKU "
                "suffix is a cross-check only.")
    row = _headers(ws, 4, ["Artist", "Amount", "Lines", "Orders"],
                   [24, 14, 8, 60])
    for artist in result.attribution.artists:
        ws.cell(row=row, column=1, value=artist.name)
        _money(ws, row, 2, artist.amount)
        ws.cell(row=row, column=3, value=artist.line_count)
        ws.cell(row=row, column=4, value=", ".join(artist.order_ids))
        row += 1
    ws.cell(row=row, column=1, value="TOTAL").font = SECTION
    _money(ws, row, 2, result.attribution.total, bold=True)
    row += 2
    if result.attribution.issues:
        row = _section(ws, row, "UNATTRIBUTED -- resolve before posting", span=4)
        for issue in result.attribution.issues:
            ws.cell(row=row, column=1, value=issue.kind)
            _money(ws, row, 2, issue.amount)
            ws.cell(row=row, column=4, value=f"{issue.order_id}: {issue.detail}")
            for col in range(1, 5):
                ws.cell(row=row, column=col).fill = FAIL_FILL
            row += 1


def _exceptions_sheet(wb, result: "Result") -> None:
    ws = wb.create_sheet("Exceptions")
    row = _headers(ws, 1, ["EX #", "Kind", "Reference", "Amount",
                           "Description", "Action", "Blocking"],
                   [8, 20, 20, 14, 70, 70, 10])
    for e in result.trail.exceptions:
        ws.cell(row=row, column=1, value=f"EX-{e.ex_num}")
        ws.cell(row=row, column=2, value=e.kind)
        ws.cell(row=row, column=3, value=e.reference)
        _money(ws, row, 4, e.amount)
        ws.cell(row=row, column=5, value=e.description)
        ws.cell(row=row, column=6, value=e.action)
        ws.cell(row=row, column=7, value="YES" if e.blocking else "no")
        if e.blocking:
            for col in range(1, 8):
                ws.cell(row=row, column=col).fill = FAIL_FILL
        row += 1
    if not result.trail.exceptions:
        ws.cell(row=row, column=2, value="None.")


def _validation_sheet(wb, result: "Result") -> None:
    ws = wb.create_sheet("Validation")
    ws["A1"] = f"Validation -- {result.business_date}"
    ws["A1"].font = TITLE
    row = _headers(ws, 3, ["Check", "Title", "Status", "Expected", "Actual",
                           "Variance", "Tolerance", "Blocking", "Detail"],
                   [22, 60, 8, 14, 14, 12, 11, 9, 90])
    fills = {"FAIL": FAIL_FILL, "WARN": WARN_FILL, "PASS": PASS_FILL}
    for check in result.report.checks:
        ws.cell(row=row, column=1, value=check.check_id)
        ws.cell(row=row, column=2, value=check.title)
        status = ws.cell(row=row, column=3, value=check.status)
        if check.status in fills:
            status.fill = fills[check.status]
        for col, value in ((4, check.expected), (5, check.actual),
                           (6, check.variance), (7, check.tolerance)):
            if value is not None:
                _money(ws, row, col, value)
        ws.cell(row=row, column=8, value="YES" if check.blocking else "no")
        ws.cell(row=row, column=9, value=check.detail)
        row += 1
