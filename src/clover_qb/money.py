"""Exact money handling.

Every monetary value in this pipeline is a ``Decimal`` quantized to cents.
Floats are never used for money: a half-cent of float drift is the difference
between a reconciliation that ties and one that does not, and this pipeline
hard-stops on variances as small as $0.05.
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

_MONEY_STRIP = re.compile(r"[^\d\-.,()]")
_PARENS = re.compile(r"^\((.*)\)$")


def q(value) -> Decimal:
    """Quantize any numeric-ish value to cents, half-up."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int,)):
        d = Decimal(value)
    elif isinstance(value, float):
        # Route through str so 0.1 does not arrive as 0.1000000000000000055.
        d = Decimal(str(value))
    else:
        d = Decimal(str(value))
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(raw, *, default: Decimal | None = ZERO) -> Decimal:
    """Parse a Clover CSV money cell.

    Handles ``$1,234.56``, ``1234.56``, ``(12.00)`` for negatives, ``CA$``
    prefixes, blank cells and ``None``. Raises ``ValueError`` on a non-empty
    cell that carries no digits, rather than silently returning zero -- a
    silently zeroed amount is the failure mode that produces a workbook that
    balances against the wrong number.
    """
    if raw is None:
        if default is None:
            raise ValueError("missing money value")
        return default
    if isinstance(raw, (int, float, Decimal)):
        return q(raw)
    text = str(raw).strip()
    if text == "" or text == "-":
        if default is None:
            raise ValueError("empty money value")
        return default
    negative = False
    m = _PARENS.match(text)
    if m:
        negative = True
        text = m.group(1)
    text = _MONEY_STRIP.sub("", text)
    if text.startswith("-"):
        negative = True
        text = text[1:]
    text = text.replace(",", "")
    if text in ("", "."):
        raise ValueError(f"unparseable money value: {raw!r}")
    value = q(Decimal(text))
    return -value if negative else value


def total(values: Iterable[Decimal]) -> Decimal:
    """Sum an iterable of cent-quantized Decimals, staying quantized."""
    acc = ZERO
    for v in values:
        acc += v
    return q(acc)


def fmt(value: Decimal) -> str:
    """Format for display: ``$1,234.56`` / ``-$1,234.56``."""
    v = q(value)
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def allocate(amount: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Split ``amount`` across ``weights`` with no lost or invented cents.

    Largest-remainder apportionment: each share gets ``floor`` of its exact
    proportion, then the leftover cents go one at a time to the largest
    remainders (ties broken by the larger weight, then by position). The
    returned list always sums to ``amount`` exactly.

    Used for split-tender orders, where one order's revenue and tax must be
    divided between (say) a card payment and a cash payment.
    """
    amount = q(amount)
    weights = [q(w) for w in weights]
    if not weights:
        raise ValueError("allocate() needs at least one weight")
    weight_sum = total(weights)
    if weight_sum == ZERO:
        # Degenerate: no payment carries weight. Put everything on the first
        # share rather than dividing by zero, and let the caller's checks see
        # it as a single-share allocation.
        return [amount] + [ZERO] * (len(weights) - 1)

    cents = int((amount / CENT).to_integral_value(rounding=ROUND_HALF_UP))
    exact = [(Decimal(cents) * w) / weight_sum for w in weights]
    floors = [int(e.to_integral_value(rounding="ROUND_FLOOR")) for e in exact]
    remainder = cents - sum(floors)
    order = sorted(
        range(len(weights)),
        key=lambda i: (exact[i] - floors[i], weights[i], -i),
        reverse=True,
    )
    step = 1 if remainder >= 0 else -1
    for k in range(abs(remainder)):
        floors[order[k % len(order)]] += step
    return [q(Decimal(c) * CENT) for c in floors]
