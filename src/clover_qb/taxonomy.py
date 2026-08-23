"""The Adrenaline unified QB Item taxonomy.

One item list serves both Adrenaline entities. The canonical names, the
colon-delimited parent/sub structure and the classification priority order are
taken from the Adrenaline Ouest (Lightspeed) chart so that a Fairview Sales
Receipt and an Ouest invoice roll up to the same QuickBooks Desktop items and
the same GL accounts.

Nothing here is inferred at runtime. If an item name is not in this module it
does not exist as far as the pipeline is concerned, and a classification that
would produce one is a hard error rather than a new item invented mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

# --- Tax codes -------------------------------------------------------------
# Quebec: GST/TPS 5%, QST/TVQ 9.975% on the GST-exclusive base.
TAX_S = "S"          # standard rated
TAX_E = "E"          # exempt / zero-rated / non-revenue
GST_RATE = Decimal("0.05")
QST_RATE = Decimal("0.09975")


@dataclass(frozen=True)
class QBItem:
    """One QuickBooks Desktop item.

    ``name`` is the full QB name including the colon path, exactly as it must
    appear in an IIF ``INVITEM`` column. ``account`` is the GL account the item
    posts to and is written into the audit trail so a reviewer can see the
    journal effect without opening QuickBooks.
    """

    name: str
    account: str
    kind: str                    # revenue | liability | contra
    default_tax_code: str = TAX_S
    per_artist: bool = False     # revenue must be attributed to an artist
    description: str = ""

    @property
    def parent(self) -> str:
        return self.name.split(":", 1)[0]

    @property
    def is_sub(self) -> bool:
        return ":" in self.name


# --- Canonical items -------------------------------------------------------

TATTOO = "Tattoo"
PIERCING = "Piercing"
PIERCING_MISC = "Piercing:Misc Services"
BODYJEWELRY = "Bodyjewelry"
BODYJEWELRY_TIT = "Bodyjewelry:Titanium Bodyjewelry"
BODYJEWELRY_14K = "Bodyjewelry:14K Bodyjewelry"
AFTERCARE = "Aftercare"
GIFT_CERTIFICATE = "Gift Certificate"
TIPS_PAYABLE = "Tips Payable"
CUSTOMER_DEPOSITS = "Customer Deposits"

ITEMS: dict[str, QBItem] = {
    item.name: item
    for item in [
        QBItem(TATTOO, "Sales:Tattoo Revenue", "revenue", TAX_S, per_artist=True,
               description="Tattoo services, attributed per artist"),
        QBItem(PIERCING, "Sales:Piercing Revenue", "revenue", TAX_S,
               description="Piercing services"),
        QBItem(PIERCING_MISC, "Sales:Piercing Revenue", "revenue", TAX_S,
               description="Insertions, downsizes, checks and other chair services"),
        QBItem(BODYJEWELRY, "Sales:Bodyjewelry Revenue", "revenue", TAX_S,
               description="Body jewellery, default merchandise item"),
        QBItem(BODYJEWELRY_TIT, "Sales:Bodyjewelry Revenue", "revenue", TAX_S,
               description="Implant-grade titanium body jewellery"),
        QBItem(BODYJEWELRY_14K, "Sales:Bodyjewelry Revenue", "revenue", TAX_S,
               description="14K gold body jewellery"),
        QBItem(AFTERCARE, "Sales:Aftercare Revenue", "revenue", TAX_S,
               description="Saline, NeilMed, Pursan and other aftercare products"),
        QBItem(GIFT_CERTIFICATE, "Gift Certificates Outstanding", "liability", TAX_E,
               description="Gift certificate sold - liability until redeemed"),
        QBItem(TIPS_PAYABLE, "Tips Payable", "liability", TAX_E,
               description="Tips collected on behalf of staff - liability until paid out"),
        QBItem(CUSTOMER_DEPOSITS, "Customer Deposits", "liability", TAX_E,
               description="Appointment deposit taken - liability until the work is done"),
    ]
}

#: Items that carry revenue. Everything else is a balance-sheet movement and is
#: excluded from the net-sales figure that reconciles against Clover.
REVENUE_ITEMS = frozenset(n for n, i in ITEMS.items() if i.kind == "revenue")

#: Retired names kept so that historical dictionary entries and prior-year
#: workbooks still resolve. ``Piercing:Stud Piercing`` was eliminated Jul 12,
#: 2026: stud, CZ and birthstone-month jewellery is merchandise, not a service.
DEPRECATED_ALIASES: dict[str, str] = {
    "Piercing:Stud Piercing": BODYJEWELRY,
    "Body Jewelry": BODYJEWELRY,
    "Bodyjewelry:Titanium": BODYJEWELRY_TIT,
    "Bodyjewelry:14K": BODYJEWELRY_14K,
    "Tips": TIPS_PAYABLE,
    "Customer Deposits - Fairview": CUSTOMER_DEPOSITS,
    "Appointment Deposit": CUSTOMER_DEPOSITS,
}


class UnknownItemError(KeyError):
    """Raised when a name is neither a canonical item nor a known alias."""


def resolve(name: str) -> QBItem:
    """Return the canonical :class:`QBItem` for ``name``.

    Accepts a canonical name or a deprecated alias. Anything else raises --
    the taxonomy is closed on purpose.
    """
    key = (name or "").strip()
    if key in ITEMS:
        return ITEMS[key]
    if key in DEPRECATED_ALIASES:
        return ITEMS[DEPRECATED_ALIASES[key]]
    raise UnknownItemError(
        f"{name!r} is not in the unified QB item taxonomy. "
        f"Known items: {', '.join(sorted(ITEMS))}"
    )


def is_known(name: str) -> bool:
    try:
        resolve(name)
    except UnknownItemError:
        return False
    return True


def sort_key(name: str) -> tuple:
    """Ordering for Sales Receipt lines.

    Revenue first (alphabetical, parents before their subs), then the
    liability items in a fixed order so the bottom of every receipt looks the
    same day to day: deposits taken, gift certificates, tips.
    """
    item = resolve(name)
    liability_order = {CUSTOMER_DEPOSITS: 0, GIFT_CERTIFICATE: 1, TIPS_PAYABLE: 2}
    if item.kind == "revenue":
        return (0, item.parent, item.is_sub, item.name)
    return (1, liability_order.get(item.name, 99), "", item.name)


@dataclass
class TaxSplit:
    """GST and QST on a base amount."""

    base: Decimal
    gst: Decimal
    qst: Decimal

    @property
    def total(self) -> Decimal:
        return self.gst + self.qst


def compute_tax(base: Decimal, tax_code: str) -> TaxSplit:
    """Quebec tax on ``base`` for the given tax code.

    Both taxes apply to the GST-exclusive base (Quebec de-harmonized QST in
    2013), so QST is *not* charged on GST.
    """
    from .money import ZERO, q

    base = q(base)
    if tax_code == TAX_S:
        return TaxSplit(base, q(base * GST_RATE), q(base * QST_RATE))
    return TaxSplit(base, ZERO, ZERO)
