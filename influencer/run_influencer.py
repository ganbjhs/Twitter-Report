#!/usr/bin/env python3
"""One command: capture every X post with engagement in frame, read its metrics,
then build the Influencer report (.pdf + .docx, A4).

    python influencer/run_influencer.py links.xlsx --title "Influencer Report"
    python influencer/run_influencer.py links.csv --workers 4 --date 25-07-26
    python influencer/run_influencer.py -                 # paste links on stdin

Deliberately mirrors the CLI surface of the frozen `run.py` (same flags, same
"<title> <date>" header, same "<Title>_<date>" output stem) so the web layer
drives both report types through one code path.

`run.py` and everything under `src/` are untouched.
"""
import datetime
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import inf_runner          # noqa: E402
import inf_report_builder  # noqa: E402


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
    title = _take_flag(argv, "--title") or "Influencer Report"
    date = _take_flag(argv, "--date") or datetime.date.today().strftime("%d-%m-%y")
    header = f"{title} {date}"
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", f"{title}_{date}").strip("_")

    # 1) capture + verify (inf_runner reads sys.argv: source / --workers / --headed)
    sys.argv = argv
    inf_runner.main()

    # 2) build the A4 .pdf + .docx
    sys.argv = ["inf_report_builder", header, stem]
    inf_report_builder.main()


if __name__ == "__main__":
    main()
