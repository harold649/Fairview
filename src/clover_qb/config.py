"""Run configuration: entity, chart-of-accounts wiring, roster, tolerances.

Everything that differs between shops, or that a human is entitled to change,
lives here. Nothing in this file is inferred at runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .money import ZERO, q

# Deposit policy.
#
# OFF_QB      appointment deposits are not on the books at all. The deposit
#             money is stripped out of the card figure so that what posts to
#             QuickBooks equals what lands in the bank, and the redemption
#             later is simply revenue.
# LIABILITY   deposits run through a Customer Deposits liability account:
#             taken today, drawn down when redeemed. The card figure is the
#             raw batch, because the deposit is on the receipt as a liability
#             line rather than being netted out of the payment.
#
# The two treatments both reconcile to zero, so no check will catch the wrong
# one being used. It is a policy choice and it must be made deliberately.
DEPOSITS_OFF_QB = "off_qb"
DEPOSITS_LIABILITY = "liability"


@dataclass
class Accounts:
    """QuickBooks Desktop account names, exactly as they appear in the chart."""

    undeposited_funds: str = "Undeposited Funds"
    bank: str = "RBC Chequing"
    tips_payable: str = "Tips Payable"
    customer_deposits: str = "Customer Deposits"
    gift_certificates: str = "Gift Certificates Outstanding"
    gst_payable: str = "GST/HST Payable"
    qst_payable: str = "QST Payable"


@dataclass
class Tolerances:
    """Variance a check will accept before it fails the run.

    ``tax`` and ``deposit_match`` are $0.05: five cents is the most that
    line-by-line rounding can plausibly produce across a day's orders, and
    anything larger is a real difference with a cause worth finding.
    ``net_sales`` and ``tattoo`` are exact -- those are additions of the same
    numbers from two directions, and a penny of difference means one of them
    is wrong.
    """

    tax: Decimal = Decimal("0.05")
    deposit_match: Decimal = Decimal("0.05")
    card_brand: Decimal = Decimal("0.05")
    tips: Decimal = Decimal("0.05")
    net_sales: Decimal = ZERO
    tattoo: Decimal = ZERO
    reconciliation: Decimal = ZERO


@dataclass
class Config:
    entity: str = "4542746 Canada Inc."
    location: str = "Adrenaline Fairview"
    qb_class: str = "Fairview"
    customer: str = "Daily Sales - Fairview"
    currency: str = "CAD"

    #: Confirmed tattoo artists. A TATTOO line under any other name is an
    #: exception -- it is never auto-attributed, and never quietly dropped.
    artist_roster: list[str] = field(default_factory=lambda: [
        "James", "Steve G", "Genesis", "Emma",
    ])
    #: Names removed from the roster, kept so the reason survives. A TATTOO
    #: line under one of these is flagged with its retirement date rather than
    #: reported as an unrecognised name.
    retired_artists: dict[str, str] = field(default_factory=lambda: {
        "Chris S": "Off the tattoo roster as of Jul 19, 2026 "
                   "(piercing, jewellery and admin only).",
    })

    accounts: Accounts = field(default_factory=Accounts)
    tolerances: Tolerances = field(default_factory=Tolerances)

    deposit_policy: str = DEPOSITS_OFF_QB
    #: Deposits taken before this date are never run through the liability
    #: account, whatever the policy says -- the account did not exist yet.
    deposit_liability_start: Optional[str] = None

    dictionary_path: Optional[str] = None
    #: Post cash to Undeposited Funds and leave it there. The physical cash is
    #: walked to the bank on its own schedule and the QuickBooks deposit is
    #: made then, against the real bank date.
    cash_to_undeposited: bool = True
    #: Escalate any item that reaches the default Bodyjewelry fallback.
    escalate_default_classifications: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        data = dict(data)
        if "accounts" in data:
            data["accounts"] = Accounts(**data["accounts"])
        if "tolerances" in data:
            data["tolerances"] = Tolerances(
                **{k: q(v) for k, v in data["tolerances"].items()})
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"unknown config key(s): {', '.join(sorted(unknown))}. "
                f"Known keys: {', '.join(sorted(known))}")
        return cls(**data)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["tolerances"] = {k: str(v) for k, v in data["tolerances"].items()}
        return data

    def is_roster_artist(self, name: str) -> bool:
        return _norm(name) in {_norm(a) for a in self.artist_roster}

    def canonical_artist(self, name: str) -> Optional[str]:
        target = _norm(name)
        for artist in self.artist_roster:
            if _norm(artist) == target:
                return artist
        return None

    def retirement_note(self, name: str) -> Optional[str]:
        for retired, note in self.retired_artists.items():
            if _norm(retired) == _norm(name):
                return note
        return None


def _norm(name: str) -> str:
    return " ".join((name or "").split()).upper()
