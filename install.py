#!/usr/bin/env python3
"""One-command setup for Report Automation — works on macOS, Windows and Linux.

    python install.py

It (idempotently):
  1. installs the Python dependencies from requirements.txt,
  2. downloads the Chromium browser Playwright drives,
  3. creates the working folders (config / sessions / reports).

Re-run it any time; it's safe to run repeatedly.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(cmd):
    print("»", " ".join(cmd), flush=True)
    subprocess.check_call(cmd)


def main():
    py = sys.executable
    print("=== Report Automation — setup ===")
    print(f"Python: {py}")

    # 1) Python dependencies
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    # 2) Browser. Chromium is the guaranteed cross-platform engine; the login
    #    flow prefers real Chrome if present but falls back to this.
    run([py, "-m", "playwright", "install", "chromium"])
    # On Linux, pull the system libraries the browser needs (no-op on mac/win).
    if sys.platform.startswith("linux"):
        try:
            run([py, "-m", "playwright", "install-deps", "chromium"])
        except Exception as e:
            print(f"(note: install-deps skipped: {e})")

    # 3) Working folders
    for d in ("config", "sessions", "reports/screenshots"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)

    print("\n✔ Setup complete.\n")
    print("Next steps:")
    print("  1) Save your X login once (a browser window opens; log in):")
    print("        python save_login.py x")
    print("  2) Put your X/Twitter post links in an Excel sheet (config/links.xlsx)")
    print("  3) Run it:")
    print("        python run.py config/links.xlsx")
    print("        python run.py -            # or paste links on stdin")


if __name__ == "__main__":
    main()
