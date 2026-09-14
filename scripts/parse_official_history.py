#!/usr/bin/env python3
"""Parse the SPIDQAH ledger spreadsheet into an aggregated monthly
official-totals payload for `flask import-official-history`.

Run this LOCALLY, on the machine that holds the private spreadsheet -
it never needs to touch the VPS. It prints a JSON report (validation
table, YTD totals, an issued/outflow reconciliation, and the final
import payload) to stdout; the spreadsheet's row-level contents are
never written anywhere by this script beyond that aggregated summary.

Usage:
    python scripts/parse_official_history.py "SPIDQAH 2026.xlsx" \
        --start 2026-01-01 --go-live 2026-09-18 > report.json

Then feed just `.payload_for_import` from that report into:
    flask import-official-history --dry-run
    flask import-official-history --apply

Expected workbook layout (see project README):
  - A "MUKULULO" sheet and a "FRI & SUN" sheet, each with header rows
    on row 1-3 and data starting row 4, main ledger columns C=RECEIVED,
    F=ISSUED, H=per-row date label (e.g. "11th/Sept/26F"). The trailing
    letter identifies Friday/Sunday on the FRI & SUN sheet; on MUKULULO
    the letter is irrelevant (Mukululo is not split by weekday) and any
    trailing note text after the date is ignored.
  - A separate "KIKUMI 100" mini-ledger inside the MUKULULO sheet is
    intentionally never read: per SPIDQAH's own accounting, its amounts
    are already folded into the main RECEIVED column, so adding them
    again would double-count Mukululo.
  - A "TOTAL RECEIVED CASH" section is a reconciliation aid only, not a
    third fund, and is never read either.

Never guesses: any row with a non-zero RECEIVED amount that cannot be
confidently parsed/classified is reported under "ambiguous_received_rows"
and excluded from the totals rather than estimated.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date

import openpyxl

MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Strict: used for the Friday/Sunday sheet, where the trailing F/S
# letter IS the fund-day classification and must be unambiguous -
# nothing else may follow it.
STRICT_DATE_RE = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?/([A-Za-z]+|\d{1,2})/(\d{2})([FS])\s*$", re.IGNORECASE)

# Lenient: used for the Mukululo sheet, where any trailing day-flag or
# free-text note is irrelevant to the fund total - only the calendar
# date matters. Only the day/month/year prefix must match.
LENIENT_DATE_RE = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?/([A-Za-z]+|\d{1,2})/(\d{2})", re.IGNORECASE)


def _build_date(day_s, month_s, year_s):
    day = int(day_s)
    if month_s.isdigit():
        month = int(month_s)
    else:
        month = MONTH_NAMES.get(month_s.strip().lower())
        if month is None:
            return None
    year = 2000 + int(year_s)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_strict(raw):
    """Returns (date, day_flag) or (None, None)."""
    if raw is None:
        return None, None
    m = STRICT_DATE_RE.match(str(raw).strip())
    if not m:
        return None, None
    day_s, month_s, year_s, flag = m.groups()
    d = _build_date(day_s, month_s, year_s)
    return (d, flag.upper()) if d else (None, None)


def parse_date_lenient(raw):
    """Returns date or None."""
    if raw is None:
        return None
    m = LENIENT_DATE_RE.match(str(raw).strip())
    if not m:
        return None
    day_s, month_s, year_s = m.groups()
    return _build_date(day_s, month_s, year_s)


def read_main_ledger(ws, received_col=3, issued_col=6, date_col=8, max_row=200, require_flag=False):
    """Reads main-ledger rows starting at row 4 (rows 1-3 are headers).
    Returns (rows, problem_rows); a problem row is one with a non-zero
    RECEIVED and/or ISSUED amount but an unparseable/unclassifiable date.
    """
    rows, problems = [], []
    for r in range(4, max_row + 1):
        received = ws.cell(row=r, column=received_col).value
        issued = ws.cell(row=r, column=issued_col).value
        raw_label = ws.cell(row=r, column=date_col).value
        if received is None and issued is None:
            continue
        received_amt = int(received) if received not in (None, "") else 0
        issued_amt = int(issued) if issued not in (None, "") else 0

        if require_flag:
            d, flag = parse_date_strict(raw_label)
        else:
            d, flag = parse_date_lenient(raw_label), None

        if d is None and (received_amt != 0 or issued_amt != 0):
            problems.append({"sheet_row": r, "raw_date_label": raw_label, "received": received_amt, "issued": issued_amt})
            continue
        if d is None:
            continue
        rows.append({"row": r, "date": d, "flag": flag, "received": received_amt, "issued": issued_amt, "raw_date_label": raw_label})
    return rows, problems


def build_report(xlsx_path, start_date, go_live_date, mukululo_sheet="MUKULULO", fri_sun_sheet="FRI & SUN"):
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    muk_ws = wb[mukululo_sheet]
    fs_ws = wb[fri_sun_sheet]

    muk_rows, muk_problems = read_main_ledger(muk_ws, require_flag=False)
    fs_rows, fs_problems = read_main_ledger(fs_ws, require_flag=True)

    ambiguous_received = [
        {"sheet": fri_sun_sheet, **r} for r in fs_rows if r["flag"] not in ("F", "S") and r["received"] != 0
    ]
    ambiguous_received += [{"sheet": mukululo_sheet, **p} for p in muk_problems if p["received"] != 0]
    ambiguous_received += [{"sheet": fri_sun_sheet, **p} for p in fs_problems if p["received"] != 0]

    unparsed_issued_only = (
        [{"sheet": mukululo_sheet, **p} for p in muk_problems if p["received"] == 0 and p["issued"] != 0]
        + [{"sheet": fri_sun_sheet, **p} for p in fs_problems if p["received"] == 0 and p["issued"] != 0]
    )

    def in_range(d):
        return start_date <= d < go_live_date

    excluded_before_start = [r for r in muk_rows + fs_rows if r["date"] < start_date]
    excluded_post_golive = [r for r in muk_rows + fs_rows if r["date"] >= go_live_date]

    muk_in_range = [r for r in muk_rows if in_range(r["date"])]
    fs_in_range = [r for r in fs_rows if in_range(r["date"])]

    monthly = defaultdict(lambda: {"mukululo": 0, "friday": 0, "sunday": 0, "mukululo_issued": 0, "fs_issued": 0})
    for r in muk_in_range:
        key = (r["date"].year, r["date"].month)
        monthly[key]["mukululo"] += r["received"]
        monthly[key]["mukululo_issued"] += r["issued"]
    for r in fs_in_range:
        key = (r["date"].year, r["date"].month)
        if r["flag"] == "F":
            monthly[key]["friday"] += r["received"]
        elif r["flag"] == "S":
            monthly[key]["sunday"] += r["received"]
        monthly[key]["fs_issued"] += r["issued"]

    months_sorted = sorted(monthly.keys())
    validation_rows, payload_months = [], []
    ytd = {"mukululo": 0, "friday": 0, "sunday": 0, "combined": 0, "overall": 0}
    issued_totals = {"mukululo_issued": 0, "fs_issued": 0}

    for (y, m) in months_sorted:
        v = monthly[(y, m)]
        combined = v["friday"] + v["sunday"]
        overall = v["mukululo"] + combined
        validation_rows.append({
            "year": y, "month": m, "mukululo": v["mukululo"], "friday": v["friday"], "sunday": v["sunday"],
            "combined": combined, "overall": overall,
            "mukululo_issued": v["mukululo_issued"], "fs_issued": v["fs_issued"],
        })
        payload_months.append({"year": y, "month": m, "mukululo": v["mukululo"], "friday": v["friday"], "sunday": v["sunday"]})
        for k in ("mukululo", "friday", "sunday"):
            ytd[k] += v[k]
        ytd["combined"] += combined
        ytd["overall"] += overall
        issued_totals["mukululo_issued"] += v["mukululo_issued"]
        issued_totals["fs_issued"] += v["fs_issued"]

    checks = []
    for row in validation_rows:
        checks.append(("Friday+Sunday=Combined", row["year"], row["month"], row["friday"] + row["sunday"] == row["combined"]))
        checks.append(("Mukululo+Combined=Overall", row["year"], row["month"], row["mukululo"] + row["combined"] == row["overall"]))

    return {
        "sheets_used": [mukululo_sheet, fri_sun_sheet],
        "start_date": start_date.isoformat(),
        "go_live_date": go_live_date.isoformat(),
        "months_imported": [f"{y}-{m:02d}" for (y, m) in months_sorted],
        "validation_rows": validation_rows,
        "ytd": ytd,
        "issued_reconciliation": {
            "mukululo_gross_received": ytd["mukululo"],
            "mukululo_issued": issued_totals["mukululo_issued"],
            "mukululo_sheet_cash_position": ytd["mukululo"] - issued_totals["mukululo_issued"],
            "fs_gross_received": ytd["combined"],
            "fs_issued": issued_totals["fs_issued"],
            "fs_sheet_cash_position": ytd["combined"] - issued_totals["fs_issued"],
            "combined_gross_received": ytd["overall"],
            "combined_issued": issued_totals["mukululo_issued"] + issued_totals["fs_issued"],
            "combined_sheet_cash_position": ytd["overall"] - (issued_totals["mukululo_issued"] + issued_totals["fs_issued"]),
        },
        "ambiguous_received_rows": ambiguous_received,
        "unparsed_issued_only_rows": unparsed_issued_only,
        "excluded_before_start_rows": [
            {"row": r["row"], "date": r["date"].isoformat(), "received": r["received"]} for r in excluded_before_start
        ],
        "excluded_post_golive_rows": [
            {"row": r["row"], "date": r["date"].isoformat(), "received": r["received"]} for r in excluded_post_golive
        ],
        "checks_all_pass": all(c[3] for c in checks),
        "failed_checks": [c for c in checks if not c[3]],
        "payload_for_import": {
            "source": f"Imported from {xlsx_path} official ledger ({mukululo_sheet} + {fri_sun_sheet} sheets)",
            "months": payload_months,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("xlsx_path", help="Path to the local spreadsheet, e.g. 'SPIDQAH 2026.xlsx'")
    parser.add_argument("--start", default="2026-01-01", help="First date to include (YYYY-MM-DD, default 2026-01-01)")
    parser.add_argument("--go-live", default="2026-09-18", help="Go-live date, exclusive (YYYY-MM-DD, default 2026-09-18)")
    parser.add_argument("--mukululo-sheet", default="MUKULULO")
    parser.add_argument("--fri-sun-sheet", default="FRI & SUN")
    args = parser.parse_args()

    report = build_report(
        args.xlsx_path,
        date.fromisoformat(args.start),
        date.fromisoformat(args.go_live),
        args.mukululo_sheet,
        args.fri_sun_sheet,
    )
    print(json.dumps(report, indent=2))

    if not report["checks_all_pass"] or report["ambiguous_received_rows"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
