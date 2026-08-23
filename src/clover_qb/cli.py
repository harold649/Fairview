"""Command-line entry point.

    clover-qb run --date 2026-08-23 \\
        --line-items LineItemsExport.csv --payments Payments.csv \\
        --tax-report TaxReport.csv --closeout Closeout.csv \\
        --employee-sales EmployeeSales.csv \\
        --config config/fairview.json --out out/2026-08-23

    clover-qb sweep --dictionary data/QB_ITEMS.txt --keyword stud --keyword cz \\
        --target Bodyjewelry --exclude tit --exclude 14k
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .dictionary import Dictionary, render_block
from .pipeline import Inputs, Result, ValidationFailed, emit, run


def _parse_approvals(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--approve expects NAME=QB Item, got: {pair!r}")
        name, item = pair.split("=", 1)
        out[name.strip()] = item.strip()
    return out


def cmd_run(args: argparse.Namespace) -> int:
    config = Config.load(args.config) if args.config else Config()
    if args.class_override:
        config.qb_class = args.class_override

    inputs = Inputs(
        line_items=args.line_items, payments=args.payments,
        tax_report=args.tax_report, closeout=args.closeout,
        employee_sales=args.employee_sales,
    )
    approvals = _parse_approvals(args.approve or [])

    result = run(inputs, config, business_date=args.date, approvals=approvals)

    try:
        written = emit(result, args.out, force=args.force,
                       tax_lines=not args.no_tax_lines,
                       workbook=not args.no_workbook)
    except ValidationFailed as exc:
        print(exc.report.render(), file=sys.stderr)
        print(f"\n{len(exc.report.failures)} check(s) failed. No files written. "
              f"Rerun with --force to write anyway (marked UNVALIDATED) for "
              f"diagnosis -- never for posting.", file=sys.stderr)
        return 1

    from .report import render_posting_guide
    print(render_posting_guide(result))
    print("Wrote:")
    for path in written:
        print(f"  {path}")
    return 0 if result.ok else 1


def cmd_sweep(args: argparse.Namespace) -> int:
    dictionary = Dictionary.load(args.dictionary)
    if args.regex:
        pattern = re.compile(args.regex, re.IGNORECASE)
    else:
        if not args.keyword:
            raise SystemExit("sweep needs --keyword (repeatable) or --regex")
        alts = "|".join(re.escape(k) for k in args.keyword)
        pattern = re.compile(alts, re.IGNORECASE)
    exclude = None
    if args.exclude:
        exclude = re.compile("|".join(re.escape(k) for k in args.exclude),
                             re.IGNORECASE)

    hits = dictionary.sweep(pattern=pattern, target=args.target, exclude=exclude)
    print(f"Sweep: {len(hits)} contradicting entr{'y' if len(hits)==1 else 'ies'} "
         f"found against target {args.target!r}")
    print("This tool checks one flat rule against one target; it does not know "
         "the full priority order. Confirm no higher-priority tier applies "
         "before drafting a correction.")
    for entry in hits:
        print(f"  line {entry.line_no:5d}  {entry.item_name!r:40s} -> "
             f"{entry.qb_item} ({entry.dated or 'undated'})")

    if args.write_block and hits:
        from datetime import date
        rows = [(e.item_name, e.qb_item, args.target) for e in hits]
        block = render_block(
            [(name, args.target, f"was {old}") for name, old, _ in rows],
            dated=date.today(), basis=args.desc or "sweep correction")
        out = Path(args.out or "sweep_correction.txt")
        out.write_text(block, encoding="utf-8")
        print(f"\nDraft correction written to {out} -- requires explicit "
             f"approval before appending to the locked dictionary.")

    return 1 if hits else 0


def cmd_check_dictionary(args: argparse.Namespace) -> int:
    dictionary = Dictionary.load(args.dictionary)
    bad = dictionary.unresolvable()
    print(f"{len(dictionary)} live entries, {len(bad)} unresolvable "
         f"(map to a name outside the taxonomy).")
    for entry in bad:
        print(f"  line {entry.line_no:5d}  {entry.item_name!r} -> "
             f"{entry.qb_item!r} (not in taxonomy)")
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clover-qb",
                                description="Clover POS to QuickBooks Desktop")
    p.add_argument("--version", action="version",
                   version=f"clover-qb {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="process one day's Clover export set")
    r.add_argument("--date", required=True, help="business date, YYYY-MM-DD")
    r.add_argument("--line-items", required=True)
    r.add_argument("--payments", required=True)
    r.add_argument("--tax-report")
    r.add_argument("--closeout")
    r.add_argument("--employee-sales")
    r.add_argument("--config")
    r.add_argument("--class-override", dest="class_override")
    r.add_argument("--out", required=True)
    r.add_argument("--approve", action="append",
                  help='NAME=QB Item, repeatable. Approves a held line for '
                       'this run only.')
    r.add_argument("--force", action="store_true",
                  help="write output even if validation fails, marked "
                       "UNVALIDATED. Diagnosis only -- never for posting.")
    r.add_argument("--no-tax-lines", action="store_true",
                  help="omit GST/QST split lines from the IIF; let "
                       "QuickBooks compute tax at the receipt screen instead")
    r.add_argument("--no-workbook", action="store_true",
                  help="skip the Excel workbook even if openpyxl is installed")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("sweep", help="find dictionary entries a rule change "
                                     "contradicts")
    s.add_argument("--dictionary", required=True)
    s.add_argument("--keyword", action="append")
    s.add_argument("--regex")
    s.add_argument("--exclude", action="append")
    s.add_argument("--target", required=True)
    s.add_argument("--desc")
    s.add_argument("--write-block", action="store_true")
    s.add_argument("--out")
    s.set_defaults(func=cmd_sweep)

    d = sub.add_parser("check-dictionary",
                       help="find dictionary entries that map outside the "
                            "taxonomy")
    d.add_argument("--dictionary", required=True)
    d.set_defaults(func=cmd_check_dictionary)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
