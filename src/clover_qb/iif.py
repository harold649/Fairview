"""QuickBooks Desktop IIF writer for Sales Receipts.

A Sales Receipt is a ``CASH SALE`` transaction in IIF. The shape is:

    TRNS   one header line -- the money coming in, debited to the deposit-to
           account, as a positive amount
    SPL    one line per revenue/liability/tax component, as negative amounts
           (credits)
    ENDTRNS

The splits must sum to the negative of the header amount or QuickBooks rejects
the whole transaction, so :func:`write` proves that before emitting anything.

Tax: GST and QST are written as explicit split lines to their payable
accounts. Canadian QuickBooks Desktop can recompute tax from a sales tax code
on import, and when it does its figure comes off the receipt subtotal rather
than per line -- which is why the reconciliation report names the override.
Pass ``tax_lines=False`` to omit the tax splits and let QuickBooks compute
them at the receipt screen instead.

IIF is tab-delimited with CRLF line endings and MM/DD/YYYY dates. None of
those are negotiable: QuickBooks silently mis-imports files that get them
wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from . import taxonomy
from .config import Config
from .money import ZERO, fmt, q, total
from .receipts import SalesReceipt

TRNS_HEADERS = ("!TRNS", "TRNSID", "TRNSTYPE", "DATE", "ACCNT", "NAME",
                "CLASS", "AMOUNT", "DOCNUM", "MEMO", "CLEAR", "TOPRINT",
                "PAYMETH")
SPL_HEADERS = ("!SPL", "SPLID", "TRNSTYPE", "DATE", "ACCNT", "NAME", "CLASS",
               "AMOUNT", "DOCNUM", "MEMO", "CLEAR", "QNTY", "PRICE",
               "INVITEM", "TAXABLE")
TRNSTYPE = "CASH SALE"


class IIFBalanceError(ValueError):
    """A transaction's splits do not sum to its header amount."""


def _iif_date(value: str) -> str:
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, pattern).strftime("%m/%d/%Y")
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")


def _amt(value: Decimal) -> str:
    return f"{q(value):.2f}"


def _clean(text: str) -> str:
    """IIF is tab-delimited and has no quoting worth relying on."""
    return (text or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def build_lines(receipts: Sequence[SalesReceipt], config: Config, *,
                tax_lines: bool = True) -> list[list[str]]:
    """Render receipts to IIF rows, proving each transaction balances."""
    rows: list[list[str]] = [list(TRNS_HEADERS), list(SPL_HEADERS), ["!ENDTRNS"]]

    for receipt in receipts:
        splits = _splits(receipt, config, tax_lines=tax_lines)
        header_amount = q(receipt.total)
        split_sum = total(s[0] for s in splits)
        if q(-split_sum) != header_amount:
            raise IIFBalanceError(
                f"{receipt.receipt_id}: splits sum to {fmt(-split_sum)} but the "
                f"receipt total is {fmt(header_amount)}. QuickBooks would "
                f"reject the import; the difference has a cause worth finding.")
        if header_amount == ZERO and not splits:
            continue

        iso = _iif_date(receipt.date)
        rows.append([
            "TRNS", "", TRNSTYPE, iso, receipt.deposit_to, receipt.customer,
            receipt.qb_class, _amt(header_amount), receipt.receipt_id,
            _clean(receipt.memo), "N", "N", _clean(receipt.payment_method),
        ])
        for amount, account, memo, item, taxable, qnty in splits:
            rows.append([
                "SPL", "", TRNSTYPE, iso, account, receipt.customer,
                receipt.qb_class, _amt(amount), receipt.receipt_id,
                _clean(memo), "N", qnty, "", _clean(item), taxable,
            ])
        rows.append(["ENDTRNS"])
    return rows


def _splits(receipt: SalesReceipt, config: Config, *, tax_lines: bool
            ) -> list[tuple[Decimal, str, str, str, str, str]]:
    """(amount, account, memo, invitem, taxable, qnty) -- credits are negative."""
    out = []
    for line in receipt.lines:
        item = taxonomy.resolve(line.qb_item)
        out.append((
            q(-line.amount),
            item.account,
            line.description or item.description,
            item.name,
            "Y" if line.tax_code == taxonomy.TAX_S else "N",
            str(line.line_count or 1),
        ))
    if tax_lines:
        if receipt.gst != ZERO:
            out.append((q(-receipt.gst), config.accounts.gst_payable,
                        "GST 5% as charged at the till", "GST", "N", "1"))
        if receipt.qst != ZERO:
            out.append((q(-receipt.qst), config.accounts.qst_payable,
                        "QST 9.975% as charged at the till", "QST", "N", "1"))
    return out


def render(receipts: Sequence[SalesReceipt], config: Config, *,
           tax_lines: bool = True) -> str:
    rows = build_lines(receipts, config, tax_lines=tax_lines)
    return "\r\n".join("\t".join(r) for r in rows) + "\r\n"


def write(path: str | Path, receipts: Sequence[SalesReceipt], config: Config,
          *, tax_lines: bool = True) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the CRLF written above survives untouched on every platform.
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(render(receipts, config, tax_lines=tax_lines))
    return p
