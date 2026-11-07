# Fairview project instructions

## Dashboard rules

```
FAIRVIEW DASHBOARD — RULES

SCOPE
- Adrenaline Fairview only (Clover POS). Ouest has its own dashboard and
  its own data file. Never read, merge or mix Ouest folders, workbooks,
  checklists or totals here.
- One dashboard: "Adrenaline Fairview — Sales to QB", a private claude.ai
  Dashboard artifact, pinned. Its data is one attached file,
  fairview_days.json. It refreshes after every Fairview daily build
  (adrenaline-fairview-daily) and on "refresh dashboard".

WHAT IT SHOWS (monthly tabs, history from Sep 1, 2026)
- Per day: status, QB invoice #, daily total, running month-to-date,
  cash deposit, note.
- Tiles: month-to-date sales, entered in QB, pending entry, missing
  reports, cash deposited, running total since Sep 1.
- Status rules:
  - Entered: workbook in Drive with a QB invoice # filled in.
  - Pending: workbook in Drive, invoice # blank.
  - Missing: past open day with no workbook.
  - Closed: closed per the checklist or a DD_closed folder.
  - Not due: today and later.
- Daily total = the workbook's grouped invoice total (pre-tax). Pending
  days count in the running totals; show the entered-only figure beside
  them.

SOURCES (list folders by ID; never by title)
- Waiting area: Sales_Reports_to_process 1cxxnW6r03t8Z1oZ9Wq041s0mU7-IkWHE
  -> MM_Fairview_To_Process/DD/
  (Oct 1r7N-e_44bpYZ_aIOnohSbFbrfb5C048q, Nov 1UBNUMJt3EoA6IGYOHQs9OuEVVOSHXXHv)
- Final area: SALES 1U6-6UK-5h9JTtfW184V40jDFiOFOhpt8 -> MM/MM_Fairview/DD/
  (Sep 09_Fairview 1Yo19qfGWq0Trfq0vn00lu70achqYbMTg,
   Oct 10_Fairview 1iRglesRQU7Mk3_3o2bXPTS4D1i1oDt_R)
- Workbooks: Adrenaline_Fairview_<Mon><DD>_<YYYY>_QB.xlsx
- Checklist: "Adrenaline Fairview - Clover POS Daily Report Extraction
  Checklist (<Month YYYY>)" in the ADRENALINE OUEST root
  1hijFEH4v7hJGVABon1oV_JOso9M1TEw6 (Nov:
  1k5BeZvX8WVjilMpELEjOZA0UjKrsfsppFHntalELMjA). Match the Fairview title
  only; the Ouest checklist sits in the same folder.
- First run: open one Fairview workbook and confirm where the invoice #
  and the grouped total sit before scanning. The Clover layout is not the
  same as Ouest's. Do not assume it.

CASH (priority: cash that hit the bank)
- Source: RBC chequing 05981 100-439-9 statements, folder
  1IJFc9ETlsbrM5xHRaQoJPr6KI40DkzMf, plus any RBC CSV after the last
  statement. Use BR TO BR branch deposits only.
- Two BR TO BR deposits on one date: the HIGHER amount is Fairview, the
  lower is Ouest. Record it on the deposit date with both amounts noted.
- One deposit, or three or more: leave unassigned, flag it, ask.
- Never derive cash from POS, Clover or card figures. Clover settlements
  (MiscPayment DP29702760018) are card, not cash.
- Known: Sep 15 $5,780.00; Oct 5 $5,400.00; Oct 26 $5,390.00; none
  Oct 27 to Nov 2.

CHECKS (every refresh)
- Order or ticket overlap between days: flag it on the earlier day, show
  the own-day adjusted total and warn of double-counted revenue in QB
  (the Ouest Oct 30 case was $2,303 counted twice).
- Two workbooks for one date: flag; use neither total.
- Checklist and folder disagree on a closed day: flag.
- Never infer, fill or carry forward a QB invoice #.

REFRESH
- Rebuild fairview_days.json, upload it to the dashboard, update the
  dataset to the new file. Do not change the page unless asked.
- Report in 1-2 lines: entered / pending / missing this month, MTD,
  running total, cash deposited, any flag.
- A dashboard failure never blocks or undoes the daily build.
```

## Open items before the first refresh

1. **Day folders.** Only month folder IDs are known. Each refresh lists
   the `DD` / `DD_closed` children of Sep (final), Oct (waiting + final)
   and Nov (waiting) by ID. Sep has no waiting-area folder ID.
2. **Workbook layout — what this repo's generator writes**
   (`src/clover_qb/workbook.py`), to verify against a real Drive file:
   - Sheet `QB Sales Receipts`: one block **per tender** (card, cash),
     each headed `<receipt_id> | <payment method> | Deposit to ...`, with
     `Subtotal` (pre-tax), GST, QST, `RECEIPT TOTAL` in column D.
   - It produces QB **Sales Receipts** (IIF `CASH SALE`), not one grouped
     invoice. `DOCNUM` is a generated receipt ID
     (`<prefix>-<YYYYMMDD>-<group>`), not a QB invoice #.
   - There is **no cell for a manually entered QB invoice #**.
   - Output filename is `<qb_class>_<stamp>_QB.xlsx` (or
     `UNVALIDATED_...`), not `Adrenaline_Fairview_<Mon><DD>_<YYYY>_QB.xlsx`.
   - Implication: if the Drive workbooks come from this pipeline,
     "daily total" = sum of `Subtotal` rows across receipts, and the
     Entered/Pending rule needs a defined place for the invoice/receipt #
     (add a cell, or read it from the checklist). If the Drive workbooks
     come from the `adrenaline-fairview-daily` skill instead, confirm its
     layout directly.
