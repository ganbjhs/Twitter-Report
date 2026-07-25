#!/usr/bin/env python3
"""One command: screenshot every X/Twitter link, verify, then build the report.

    python run.py config/links.xlsx          # an Excel sheet of X links
    python run.py -                          # paste links on stdin, Ctrl-D
    python run.py --workers 8                # more parallel workers
    python run.py --date 25-07-26            # date shown in the report header (dd-mm-yy)
    python run.py --title "Twitter Report"   # header label (default: "Twitter Report")
    python run.py --headed                   # watch the browser

The report header reads "<title> <date>", e.g. "Twitter Report 25-07-26"; the
date defaults to today (dd-mm-yy) when --date is omitted.

Under the hood this runs the capture + verification pass (src/run_report.py)
and then the report builder (src/report_builder.py), writing the .pdf and .docx
to the reports/ folder.
"""
import datetime
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import run_report  # noqa: E402
import report_builder  # noqa: E402


def _take_flag(argv, flag):
    """Pop '--flag value' out of argv, returning value (or None)."""
    if flag in argv:
        i = argv.index(flag)
        val = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
        return val
    return None


def main():
    argv = sys.argv[:]
    title = _take_flag(argv, "--title") or "Twitter Report"
    date = _take_flag(argv, "--date") or datetime.date.today().strftime("%d-%m-%y")
    header = f"{title} {date}"
    # file name = "<Title>_<date>", filesystem-safe (e.g. Twitter_Report_25-07-26)
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", f"{title}_{date}").strip("_")

    # 1) capture + verify — run_report reads sys.argv (source / --workers / --headed)
    sys.argv = argv
    run_report.main()

    # 2) build the .pdf + .docx report (header text, output file stem)
    sys.argv = ["report_builder", header, stem]
    report_builder.main()


if __name__ == "__main__":
    main()
