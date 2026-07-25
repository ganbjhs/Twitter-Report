#!/usr/bin/env python3
"""Save your X/Twitter login so runs can reuse it (one-time).

    python save_login.py x

A real browser window opens on X's login page. Log in by hand (username +
password; avoid "Continue with Google"). The session saves itself the moment
you're in, to sessions/x_state.json.

Re-run whenever X starts hitting login walls (sessions expire).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import save_sessions  # noqa: E402

if __name__ == "__main__":
    save_sessions.main()
