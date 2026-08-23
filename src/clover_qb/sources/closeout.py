"""``Closeout`` -- the card batch anchor.

Whichever format was provided is the anchor; there is no CSV-primary /
PDF-fallback hierarchy. When both formats exist for the same day they describe
one batch: parse the CSV, and leave the PDF alone unless a specific flagged
exception needs investigating. A disagreement between the two is an exception
for a human, never a tiebreak to perform silently.

PDF support needs PyMuPDF (``pip install 'clover-qb[pdf]'``). Without it a PDF
closeout raises rather than silently falling back to another source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..money import ZERO, parse_money, total
from .csvutil import norm_header, read_raw

_BATCH_ID = re.compile(r"#?\s*([A-Z0-9]{8,})")
_MONEY = re.compile(r"-?\$?\s*[\d,]+\.\d{2}")
_KNOWN_BRANDS = ("VISA", "MASTERCARD", "AMERICAN EXPRESS", "AMEX", "INTERAC",
                 "DISCOVER", "DEBIT", "JCB", "DINERS", "UNIONPAY")


@dataclass
class CardType:
    name: str
    count: int
    amount: Decimal


@dataclass
class Closeout:
    path: Path
    source: str                       # "csv" | "pdf"
    batch_id: str = ""
    batch_total: Decimal = ZERO
    transaction_count: int = 0
    card_types: list[CardType] = field(default_factory=list)
    employee_totals: dict[str, Decimal] = field(default_factory=dict)
    parse_notes: list[str] = field(default_factory=list)

    @property
    def card_type_total(self) -> Decimal:
        return total(ct.amount for ct in self.card_types)


class CloseoutParseError(Exception):
    """The closeout was supplied but could not be read.

    Deliberately distinct from "no closeout supplied": a file that fails to
    parse is an exception for a human, not a cue to switch to the other format
    or to another source.
    """


def load(path: str | Path) -> Closeout:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return _load_pdf(p)
    return _load_csv(p)


def _is_section(cell: str, *names: str) -> bool:
    key = norm_header(cell)
    return any(key.startswith(norm_header(n)) for n in names)


def _load_csv(path: Path) -> Closeout:
    rows = read_raw(path)
    if not rows:
        raise CloseoutParseError(f"{path.name} is empty")
    out = Closeout(path, "csv")
    section = ""
    for row in rows:
        if not any(c.strip() for c in row):
            section = ""
            continue
        head = row[0]
        if _is_section(head, "card type totals", "card types"):
            section = "cards"
            continue
        if _is_section(head, "employee totals", "employees"):
            section = "employees"
            continue
        if _is_section(head, "batch totals", "batch summary", "totals"):
            section = "batch"
            continue

        joined = " ".join(row)
        if not out.batch_id:
            for cell in row:
                if norm_header(cell).startswith(("batch id", "batch")):
                    m = _BATCH_ID.search(joined.upper().replace("BATCH ID", ""))
                    if m:
                        out.batch_id = "#" + m.group(1)
                    break

        if section == "cards":
            name = head.strip()
            if not name or norm_header(name) in ("card type", "type", "brand"):
                continue
            count, amount = _count_and_amount(row)
            if amount is not None:
                out.card_types.append(CardType(name, count, amount))
        elif section == "employees":
            name = head.strip()
            if not name or norm_header(name) in ("employee", "name"):
                continue
            _, amount = _count_and_amount(row)
            if amount is not None:
                out.employee_totals[name] = amount
        elif section == "batch" or not section:
            key = norm_header(head)
            if key.startswith(("total", "batch total", "net total", "grand total")):
                _, amount = _count_and_amount(row)
                if amount is not None and out.batch_total == ZERO:
                    out.batch_total = amount
            if key.startswith(("transactions", "transaction count", "count")):
                count, _ = _count_and_amount(row)
                out.transaction_count = out.transaction_count or count

    if out.batch_total == ZERO and out.card_types:
        out.batch_total = out.card_type_total
        out.parse_notes.append(
            "No explicit batch total row; batch total derived from the sum of "
            "CARD TYPE TOTALS.")
    if out.batch_total == ZERO:
        raise CloseoutParseError(
            f"{path.name} parsed but yielded no batch total. This is an "
            f"exception to resolve, not a reason to switch source.")
    if not out.batch_id:
        out.parse_notes.append("Batch ID absent from the closeout.")
    return out


def _count_and_amount(row: list[str]) -> tuple[int, Decimal | None]:
    count = 0
    amount = None
    for cell in row[1:]:
        text = (cell or "").strip()
        if not text:
            continue
        if _MONEY.fullmatch(text.replace(" ", "")) or "$" in text:
            if amount is None:
                amount = parse_money(text)
        elif text.replace(",", "").isdigit() and not count:
            count = int(text.replace(",", ""))
    if amount is None:
        # Last resort: a bare decimal with no currency marker.
        for cell in row[1:]:
            if re.fullmatch(r"-?[\d,]+\.\d{2}", (cell or "").strip()):
                amount = parse_money(cell)
                break
    return count, amount


def _load_pdf(path: Path) -> Closeout:
    try:
        import fitz  # type: ignore
    except ImportError as exc:                       # pragma: no cover
        raise CloseoutParseError(
            "A PDF closeout was supplied but PyMuPDF is not installed. "
            "Install with: pip install 'clover-qb[pdf]'. Do not substitute "
            "another source for the batch anchor."
        ) from exc

    doc = fitz.open(path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    if not text.strip():                             # pragma: no cover
        raise CloseoutParseError(
            f"{path.name} has no text layer. Render it to an image and read "
            f"the figures manually, then supply them as a closeout CSV -- do "
            f"not guess and do not fall back to the Payments export.")

    out = Closeout(path, "pdf")
    m = _BATCH_ID.search(text.upper().split("BATCH", 1)[-1][:60]) if "BATCH" in text.upper() else None
    if m:
        out.batch_id = "#" + m.group(1)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        money = _MONEY.findall(line)
        if not money:
            continue
        amount = parse_money(money[-1])
        for brand in _KNOWN_BRANDS:
            if upper.startswith(brand):
                counts = re.findall(r"\b(\d+)\b", line)
                out.card_types.append(
                    CardType(line[:len(brand)].title() if brand != "AMEX" else "Amex",
                             int(counts[0]) if counts else 0, amount))
                break
        else:
            if re.match(r"(NET |BATCH |GRAND )?TOTAL", upper) and out.batch_total == ZERO:
                out.batch_total = amount

    if out.batch_total == ZERO and out.card_types:
        out.batch_total = out.card_type_total
        out.parse_notes.append(
            "No total line found in the PDF; batch total derived from card "
            "type lines.")
    if out.batch_total == ZERO:
        raise CloseoutParseError(
            f"{path.name} parsed but yielded no batch total.")
    return out
