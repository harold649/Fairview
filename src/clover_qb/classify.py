"""Item-name classification into the unified QB item taxonomy.

Priority order, first match wins. The keyword tiers and their relative order
are the Adrenaline Ouest order, so a name classifies identically at either
shop; the Fairview-specific tiers (deposits, gift certificates, custom items)
sit above them because those names carry no keywords a lower tier would catch.

Two rules that look like exceptions and are not:

* Stud / CZ / birthstone-month jewellery resolves to ``Bodyjewelry``. That is
  a confirmed rule as of Jul 12, 2026, not a fallback, so it does not
  escalate. It gets its own tier purely so the audit trail records *why* the
  item landed on Bodyjewelry rather than logging "default".
* ``Custom Item`` and manual entries with no Clover Item ID never classify
  automatically, no matter how familiar the amount or the employee. They are
  held for human approval every time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from . import taxonomy
from .dictionary import Dictionary, normalize

# Names that must never resolve from the locked dictionary, because the same
# literal name is reused for unrelated sales.
NEVER_DICTIONARY = frozenset({"CUSTOM ITEM", "CUSTOM AMOUNT", "MANUAL ENTRY", ""})

_MONTHS = (
    "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC"
    "|JANUARY|FEBRUARY|MARCH|APRIL|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER"
    "|NOVEMBER|DECEMBER"
)


@dataclass(frozen=True)
class Decision:
    """The outcome of classifying one item name, with its reasoning."""

    item_name: str
    qb_item: Optional[str]
    tax_code: str
    rule_id: str
    priority: int
    rationale: str
    escalate: bool = False   # unfamiliar -- surface to the reviewer
    hold: bool = False       # cannot post until a human approves it
    proposal: Optional[str] = None   # recommended item for a held line

    @property
    def postable(self) -> bool:
        return self.qb_item is not None and not self.hold

    @property
    def recommended(self) -> Optional[str]:
        """What to show a reviewer: the posting item, or the proposal."""
        return self.qb_item or self.proposal


@dataclass(frozen=True)
class Rule:
    rule_id: str
    priority: int
    qb_item: Optional[str]
    test: Callable[[str], bool]
    rationale: str
    escalate: bool = False
    hold: bool = False


def _kw(*words: str) -> Callable[[str], bool]:
    """Substring match on the normalized (uppercased) name."""
    return lambda name: any(w in name for w in words)


def _re(pattern: str) -> Callable[[str], bool]:
    rx = re.compile(pattern)
    return lambda name: bool(rx.search(name))


# --- The ladder ------------------------------------------------------------
# Priority 0 is the session override and 1 is the locked dictionary; both are
# handled in Classifier.classify() because they need state. Everything from 2
# down is a pure function of the name.

RULES: list[Rule] = [
    Rule("custom_item", 2, None,
         lambda n: n in ("CUSTOM ITEM", "CUSTOM AMOUNT", "MANUAL ENTRY"),
         "Custom Item requires explicit approval before posting",
         escalate=True, hold=True),
    Rule("tattoo", 3, taxonomy.TATTOO,
         _re(r"^TATTOO(\s|-|$)"),
         "TATTOO prefix (plain item or per-artist SKU)"),
    Rule("appointment_deposit", 4, taxonomy.CUSTOMER_DEPOSITS,
         _kw("APPOINTMENT DEPOSIT", "APPT DEPOSIT", "DEPOSIT"),
         "Appointment deposit - liability, not revenue"),
    Rule("gift_certificate", 5, taxonomy.GIFT_CERTIFICATE,
         _kw("GIFT CERT", "GIFT CARD", "GIFTCARD", "CERTIFICAT CADEAU",
             "CARTE CADEAU"),
         "Gift certificate sold - liability until redeemed"),
    Rule("piercing", 6, taxonomy.PIERCING,
         _kw("PIERCING", "PERCAGE", "PERÇAGE"),
         "PIERCING keyword"),
    Rule("gold_14k", 7, taxonomy.BODYJEWELRY_14K,
         _re(r"\b14\s*K\b|14KT|14 KARAT"),
         "14K keyword"),
    Rule("titanium", 8, taxonomy.BODYJEWELRY_TIT,
         _re(r"\bTIT\b|TITANIUM|IND\.?\s*STRENGTH|INDUSTRIAL\s+STRENGTH"),
         "TIT/TITANIUM keyword, or the paired Industrial Strength brand form"),
    Rule("stud_cz", 9, taxonomy.BODYJEWELRY,
         _re(r"\bSTUD\b|\bCZ\b|\bBIRTHSTONE\b|(" + _MONTHS + r")\s+STUD"),
         "Stud/CZ/birthstone jewellery is merchandise (rule confirmed "
         "Jul 12, 2026, supersedes Piercing:Stud Piercing)"),
    Rule("aftercare", 10, taxonomy.AFTERCARE,
         _kw("AFTERCARE", "NEILMED", "PURSAN", "SALINE", "CARE"),
         "Aftercare product keyword"),
    Rule("misc_service", 11, taxonomy.PIERCING_MISC,
         _kw("INSERTION", "DOWNSIZE", "REMOVAL", "CHANGE OUT", "CHECK UP",
             "CONSULT"),
         "Chair service performed without a piercing charge"),
]

DEFAULT_RULE = Rule(
    "default_bodyjewelry", 99, taxonomy.BODYJEWELRY, lambda n: True,
    "No keyword matched - defaults to Bodyjewelry, unfamiliar SKU",
    escalate=True,
)


class Classifier:
    """Applies the priority ladder against a dictionary and session overrides."""

    def __init__(self, dictionary: Optional[Dictionary] = None,
                 overrides: Optional[dict[str, str]] = None,
                 *, escalate_defaults: bool = True):
        self.dictionary = dictionary or Dictionary.empty()
        self.overrides = {normalize(k): v for k, v in (overrides or {}).items()}
        self.escalate_defaults = escalate_defaults

    def classify(self, item_name: str, *, item_id: str = "") -> Decision:
        raw = item_name or ""
        name = normalize(raw)

        # Priority 0 -- explicit approval for this run. Beats everything,
        # including the hold on Custom Items: an approval IS the resolution.
        if name in self.overrides or self._override_key(raw, item_id):
            key = self._override_key(raw, item_id) or name
            target = self.overrides[key]
            item = taxonomy.resolve(target)
            return Decision(raw, item.name, item.default_tax_code,
                            "session_override", 0,
                            f"Approved for this run as {item.name}")

        # Priority 1 -- the locked dictionary.
        if name not in NEVER_DICTIONARY:
            qb_item = self.dictionary.qb_item(raw)
            if qb_item:
                entry = self.dictionary.lookup(raw)
                item = taxonomy.resolve(qb_item)
                return Decision(raw, item.name, item.default_tax_code,
                                "locked_dictionary", 1,
                                f"Locked dictionary match "
                                f"(line {entry.line_no}, {entry.dated or 'undated'})")

        # Keyword ladder. Run it even for lines that will be held, so the
        # reviewer gets a recommendation rather than a blank.
        decision = None
        for rule in RULES:
            if rule.test(name):
                decision = self._from_rule(raw, rule)
                break
        if decision is None:
            decision = self._from_rule(raw, DEFAULT_RULE)
            if not self.escalate_defaults:
                decision = replace(decision, escalate=False)

        # A line with no Clover Item ID was typed at the till. It is a Custom
        # Item by another name and holds the same way, whatever it was called.
        if not item_id and not decision.hold:
            return replace(
                decision,
                qb_item=None, proposal=decision.qb_item,
                rule_id="manual_entry", priority=2,
                rationale=("Manual entry, no Clover Item ID - requires approval. "
                           f"Proposed {decision.qb_item} ({decision.rationale})"),
                escalate=True, hold=True,
            )
        return decision

    # -- helpers ------------------------------------------------------------

    def _override_key(self, raw: str, item_id: str) -> Optional[str]:
        """Overrides may be keyed by item id, to disambiguate Custom Items."""
        for key in (f"ID:{item_id}" if item_id else None, normalize(raw)):
            if key and key in self.overrides:
                return key
        return None

    def _from_rule(self, raw: str, rule: Rule) -> Decision:
        if rule.qb_item is None:
            proposal = None
            for candidate in RULES:
                if candidate.qb_item and candidate.test(normalize(raw)):
                    proposal = taxonomy.resolve(candidate.qb_item).name
                    break
            return Decision(raw, None, taxonomy.TAX_S, rule.rule_id,
                            rule.priority, rule.rationale,
                            escalate=rule.escalate, hold=rule.hold,
                            proposal=proposal)
        item = taxonomy.resolve(rule.qb_item)
        return Decision(raw, item.name, item.default_tax_code, rule.rule_id,
                        rule.priority, rule.rationale,
                        escalate=rule.escalate, hold=rule.hold)


def rule_table() -> list[tuple[int, str, str, str]]:
    """The ladder, for documentation and for the audit trail header."""
    rows = [(0, "session_override", "(as approved)", "Explicit approval for this run"),
            (1, "locked_dictionary", "(per dictionary)", "Exact item-name match in QB_ITEMS.txt")]
    rows += [(r.priority, r.rule_id, r.qb_item or "HOLD", r.rationale) for r in RULES]
    rows.append((DEFAULT_RULE.priority, DEFAULT_RULE.rule_id,
                 DEFAULT_RULE.qb_item, DEFAULT_RULE.rationale))
    return rows
