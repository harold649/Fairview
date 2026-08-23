"""The audit trail.

The point of this module is that a year from now someone can take any figure
on a Sales Receipt and walk it back to the row of the Clover export it came
from, and see which classification rule put it there and why.

Four artefacts per run:

``run.json``        manifest: inputs with SHA-256 digests and row counts, the
                    configuration in force, every figure the run produced and
                    every check it ran.
``line_trace.csv``  one row per source line per tender group. Source row
                    number, order, employee, item, amount, the rule that fired
                    and its priority, the QB item, the tax split, the tender
                    it was paid by, and the receipt line it landed on.
``decisions.csv``   one row per distinct item name seen, with the rule that
                    classified it. This is what a dictionary update is built
                    from.
``exceptions.csv``  everything a human has to decide or acknowledge.

The digests matter: they are what distinguishes "we reran the same day and got
the same answer" from "we reran the day and the export had changed underneath
us".
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from . import taxonomy
from .money import ZERO, q

TOOL = "clover-qb"
VERSION = "1.0.0"


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class InputFile:
    role: str
    path: str
    sha256: str
    bytes: int
    rows: Optional[int] = None

    @classmethod
    def of(cls, role: str, path: str | Path, rows: Optional[int] = None) -> "InputFile":
        p = Path(path)
        return cls(role, str(p), sha256(p), p.stat().st_size, rows)


@dataclass
class TraceRow:
    """One source line, as it travelled through the pipeline."""

    source_row: int
    order_id: str
    order_date: str
    employee: str
    item_name: str
    item_id: str
    source_amount: Decimal
    allocated_amount: Decimal
    allocation_share: str
    split_tender: bool
    tender_group: str
    payment_ids: str
    rule_id: str
    rule_priority: int
    rule_rationale: str
    qb_item: str
    qb_account: str
    tax_code: str
    gst: Decimal
    qst: Decimal
    artist: str
    receipt_id: str
    status: str            # posted | held | unattributed | unpaid | refunded | open
    note: str = ""


@dataclass
class DecisionRow:
    item_name: str
    item_id: str
    occurrences: int
    amount: Decimal
    qb_item: str
    proposal: str
    rule_id: str
    rule_priority: int
    rationale: str
    escalate: bool
    hold: bool


@dataclass
class ExceptionRow:
    ex_num: int
    kind: str
    reference: str
    amount: Decimal
    description: str
    action: str
    blocking: bool


@dataclass
class AuditTrail:
    run_id: str
    started_at: str
    business_date: str
    tool: str = TOOL
    version: str = VERSION
    config: dict = field(default_factory=dict)
    inputs: list[InputFile] = field(default_factory=list)
    traces: list[TraceRow] = field(default_factory=list)
    decisions: list[DecisionRow] = field(default_factory=list)
    exceptions: list[ExceptionRow] = field(default_factory=list)
    figures: dict[str, Any] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, business_date: str, config: dict) -> "AuditTrail":
        now = datetime.now(timezone.utc)
        return cls(
            run_id=f"{business_date}-{now.strftime('%Y%m%dT%H%M%SZ')}",
            started_at=now.isoformat(timespec="seconds"),
            business_date=business_date,
            config=config,
        )

    def add_exception(self, kind: str, reference: str, amount: Decimal,
                      description: str, action: str, *, blocking: bool) -> None:
        self.exceptions.append(ExceptionRow(
            len(self.exceptions) + 1, kind, reference, q(amount), description,
            action, blocking))

    # -- emit ---------------------------------------------------------------

    def write(self, directory: str | Path) -> list[Path]:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        written = [
            _write_csv(out / "line_trace.csv", self.traces),
            _write_csv(out / "decisions.csv", self.decisions),
            _write_csv(out / "exceptions.csv", self.exceptions),
        ]
        manifest = out / "run.json"
        manifest.write_text(
            json.dumps(self._manifest(), indent=2, default=_json_default),
            encoding="utf-8")
        written.append(manifest)
        return written

    def _manifest(self) -> dict:
        return {
            "tool": self.tool,
            "version": self.version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "business_date": self.business_date,
            "config": self.config,
            "inputs": [asdict(i) for i in self.inputs],
            "figures": self.figures,
            "checks": self.checks,
            "exceptions": [asdict(e) for e in self.exceptions],
            "outputs": self.outputs,
            "counts": {
                "trace_rows": len(self.traces),
                "distinct_items": len(self.decisions),
                "exceptions": len(self.exceptions),
                "blocking_exceptions": sum(1 for e in self.exceptions if e.blocking),
            },
        }


def _json_default(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _write_csv(path: Path, rows: Sequence[Any]) -> Path:
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    fields = list(asdict(rows[0]).keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            record = asdict(row)
            writer.writerow({k: _cell(v) for k, v in record.items()})
    return path


def _cell(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value
