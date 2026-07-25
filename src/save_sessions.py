"""Save a logged-in X/Twitter session so run_report.py can reuse it.

The runner loads sessions/x_state.json (Playwright storage_state) instead of
logging in every run. This script produces that file.

Usage:
    python src/save_sessions.py x           # log into X -> sessions/x_state.json

How it works (default = manual login):
    A real Chrome window opens on that platform's login page, with the
    automation tells removed so the site doesn't falsely flag you as a bot.
    Log in by hand — the session auto-saves the moment your auth cookie appears.
    TIP: use the site's own username + password. "Sign in with Google" is the
    most likely thing to be blocked in a non-standard browser.

Only that platform's cookies are written, so each session file stays minimal.

These files contain live login cookies — treat them like passwords. Don't
commit them; add sessions/ to .gitignore.
"""
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platforms import PLATFORMS, domain_matches  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def session_file(platform: str) -> Path:
    return ROOT / "sessions" / f"{platform}_state.json"


def _filter_state(state: dict, platform: str) -> dict:
    """Keep only this platform's cookies/origins — don't dump the whole jar."""
    cookies = [c for c in state.get("cookies", [])
               if domain_matches(c.get("domain", ""), platform)]
    origins = [o for o in state.get("origins", [])
               if domain_matches(o.get("origin", ""), platform)]
    return {"cookies": cookies, "origins": origins}


def _write_state(state: dict, platform: str) -> None:
    filtered = _filter_state(state, platform)
    out = session_file(platform)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(filtered, indent=2))
    n = len(filtered["cookies"])
    print(f"[save] wrote {n} {platform} cookie(s) -> {out}")
    if n == 0:
        print(f"[save] WARNING: no {platform} cookies captured — the login "
              "probably didn't complete. Re-run and finish signing in.")


def _has_auth_cookie(ctx, platform: str) -> bool:
    want = PLATFORMS[platform]["auth_cookie"]
    return any(c["name"] == want and domain_matches(c.get("domain", ""), platform)
               for c in ctx.cookies())


def save_manual(platform: str, timeout_s: int = 600) -> None:
    """Open real Chrome (automation tells removed) on the platform login page,
    let the user log in, then save storage_state from that same window."""
    cfg = PLATFORMS[platform]
    login_profile = ROOT / "sessions" / f".chrome-login-{platform}"
    login_profile.mkdir(parents=True, exist_ok=True)

    print(f"[save] opening a Chrome window on {cfg['login_url']} ...")
    print("[save] TIP: log in with your USERNAME + PASSWORD. Avoid 'Sign in "
          "with Google/Facebook' — those are strictest about non-standard browsers.")
    print("[save] This auto-saves the moment you're in; no need to come back here.")

    with sync_playwright() as p:
        common = dict(
            headless=False,
            viewport=None,  # real window size — looks less automated
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
        )
        # Prefer real Chrome (best anti-bot profile); fall back to the bundled
        # Chromium so this works on any machine, Chrome installed or not.
        try:
            ctx = p.chromium.launch_persistent_context(
                str(login_profile), channel="chrome", **common)
        except Exception:
            print("[save] Google Chrome not found — using bundled Chromium")
            ctx = p.chromium.launch_persistent_context(str(login_profile), **common)
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(cfg["login_url"], wait_until="domcontentloaded")

        waited = 0
        while waited < timeout_s:
            if _has_auth_cookie(ctx, platform):
                page.wait_for_timeout(2500)  # let secondary cookies/localStorage settle
                _write_state(ctx.storage_state(), platform)
                ctx.close()
                return
            page.wait_for_timeout(2000)
            waited += 2

        print(f"[save] timed out after {timeout_s}s waiting for login — re-run when ready.")
        _write_state(ctx.storage_state(), platform)
        ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Save a logged-in social session for run_report.py")
    ap.add_argument("platform", nargs="?", default="x",
                    choices=sorted(PLATFORMS.keys()),
                    help="which platform to log into (default: x)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds to wait for you to finish logging in (default: 600)")
    args = ap.parse_args()
    save_manual(args.platform, args.timeout)


if __name__ == "__main__":
    main()
