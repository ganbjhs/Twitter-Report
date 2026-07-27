"""Headless X sign-in for the shared capture account (§C auto-relogin).

Why this exists: on a free host the filesystem is ephemeral, so
`sessions/x_state.json` disappears on every restart or rebuild. Rather than make
an admin re-upload a cookie file each time, the server signs in by itself using
the shared account's credentials from the environment and regenerates the file.

The cookie file stays the format the frozen pipeline already reads
(`{"cookies": [...], "origins": [...]}`), so `src/run_report.py` and the
influencer runner pick it up with no changes.

Credentials come only from the environment / Space secrets and are never logged,
never returned by an API, and never sent to the browser.

    ensure_session()   -> (ok, message)   sign in only if the cookie is missing/stale
    force_login()      -> (ok, message)   sign in unconditionally
    session_state()    -> dict            what the admin page shows
"""
import json
import threading
import time

from . import config

# X's login flow, step by step. Each step is "wait for a field, fill it, submit".
_LOGIN_URL = "https://x.com/i/flow/login"
_HOME_URL = "https://x.com/home"

_USER_INPUT = 'input[autocomplete="username"], input[name="text"]'
_PASSWORD_INPUT = 'input[autocomplete="current-password"], input[name="password"]'
_CHALLENGE_INPUT = ('input[data-testid="ocfEnterTextTextInput"], '
                    'input[name="text"]')
_TOTP_INPUT = 'input[data-testid="ocfEnterTextTextInput"], input[name="text"]'
_LOGIN_BUTTON = '[data-testid="LoginForm_Login_Button"]'

# Text X shows when it wants the account's email/phone before the password.
_CHALLENGE_HINTS = (
    "enter your phone number or email address",
    "enter your phone number or username",
    "there was unusual login activity",
    "help us keep your account safe",
)
_TOTP_HINTS = ("enter your verification code", "two-factor", "authentication code",
               "enter the code")
_BAD_CREDS_HINTS = ("wrong password", "incorrect. please try again",
                    "the username and password you entered did not match")
_RATE_HINTS = ("could not log you in now", "too many", "try again later",
               "suspicious login")

_STEP_TIMEOUT = 20000

# One login at a time — several jobs can start at once and must not each open a
# browser and race to write the same file.
_lock = threading.Lock()
_last_attempt = {"at": 0.0, "ok": False, "message": "never attempted"}
_MIN_RETRY_SECONDS = 90     # don't hammer X if the credentials are wrong


# --------------------------------------------------------------------------- #
# Cookie file
# --------------------------------------------------------------------------- #
def _read_state():
    f = config.X_STATE_FILE
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return None


def auth_cookie(state=None):
    """The auth_token cookie dict, or None."""
    state = state if state is not None else _read_state()
    if not state:
        return None
    for c in state.get("cookies", []):
        if c.get("name") == "auth_token":
            return c
    return None


def session_is_valid(min_hours_left: int = 6) -> bool:
    """True when a usable, non-imminently-expiring login cookie is on disk."""
    cookie = auth_cookie()
    if not cookie:
        return False
    expires = cookie.get("expires", -1)
    if expires and expires > 0:
        return expires - time.time() > min_hours_left * 3600
    return True                       # session cookie with no expiry — assume live


def credentials_configured() -> bool:
    return bool(config.X_USERNAME and config.X_PASSWORD)


def invalidate(reason: str = "") -> bool:
    """Throw away the stored cookie so the next job signs in again.

    Called when captures come back with login walls: the cookie is present but X
    is not accepting it.

    Only deletes the file when the server can actually recreate it. Without
    credentials the file is a hand-uploaded artefact — deleting it would leave
    the admin with nothing and no way back, which is worse than keeping a cookie
    that might still work for some posts.
    """
    _last_attempt["ok"] = False
    _last_attempt["message"] = reason or "session invalidated"
    if not credentials_configured():
        return False
    try:
        config.X_STATE_FILE.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _write_state(storage_state: dict) -> int:
    """Persist only X's cookies/origins, like save_sessions.py does."""
    domains = ("x.com", "twitter.com")

    def keep(value):
        return any(d in (value or "") for d in domains)

    filtered = {
        "cookies": [c for c in storage_state.get("cookies", [])
                    if keep(c.get("domain", ""))],
        "origins": [o for o in storage_state.get("origins", [])
                    if keep(o.get("origin", ""))],
    }
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.X_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(filtered, indent=2))
    tmp.replace(config.X_STATE_FILE)        # atomic: a job never reads a half file
    return len(filtered["cookies"])


# --------------------------------------------------------------------------- #
# The sign-in flow
# --------------------------------------------------------------------------- #
def _page_text(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _fill_and_submit(page, selector, value, timeout=_STEP_TIMEOUT) -> bool:
    """Type `value` into the first VISIBLE field matching `selector`, submit it.

    X keeps hidden inputs in the DOM, so plain `.first` can resolve to one of
    those and time out while the real field is sitting right there — hence
    `visible=true` and a per-alternative fallback.
    """
    deadline = time.time() + timeout / 1000.0
    alternatives = [s.strip() for s in selector.split(",") if s.strip()]
    while time.time() < deadline:
        for alt in alternatives:
            try:
                field = page.locator(alt).locator("visible=true").first
                if not field.count():
                    continue
                field.click(timeout=4000)
                field.fill(value, timeout=4000)
                page.wait_for_timeout(300)
                field.press("Enter")
                page.wait_for_timeout(2500)
                return True
            except Exception:
                continue
        page.wait_for_timeout(700)
    return False


def _totp_code() -> str:
    if not config.X_TOTP_SECRET:
        return ""
    try:
        import pyotp
    except ImportError:
        print("[x-login] 2FA is configured but pyotp is not installed", flush=True)
        return ""
    try:
        return pyotp.TOTP(config.X_TOTP_SECRET.replace(" ", "")).now()
    except Exception as e:
        print(f"[x-login] could not generate a 2FA code: {e}", flush=True)
        return ""


def _signed_in(ctx) -> bool:
    return any(c["name"] == "auth_token" for c in ctx.cookies())


def _do_login(headless: bool = True):
    """Drive the browser through X's login. Returns (ok, message)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 1000},
            locale="en-IN",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"))
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        try:
            page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            # 1) username
            if not _fill_and_submit(page, _USER_INPUT, config.X_USERNAME):
                return False, "X did not show the username field."

            # 2) X sometimes interrupts with "confirm your email/phone"
            text = _page_text(page)
            if any(h in text for h in _CHALLENGE_HINTS):
                if not config.X_EMAIL:
                    return False, ("X asked to confirm the account's email or "
                                   "phone, but X_EMAIL is not set.")
                _fill_and_submit(page, _CHALLENGE_INPUT, config.X_EMAIL)
                text = _page_text(page)

            # 3) password
            if not _fill_and_submit(page, _PASSWORD_INPUT, config.X_PASSWORD):
                return False, "X did not show the password field."
            try:
                page.locator(_LOGIN_BUTTON).first.click(timeout=3000)
            except Exception:
                pass                      # Enter already submitted it
            page.wait_for_timeout(3500)

            # 4) two-factor, if the account has it
            text = _page_text(page)
            if not _signed_in(ctx) and any(h in text for h in _TOTP_HINTS):
                code = _totp_code()
                if not code:
                    return False, ("The account asks for a 2FA code. Set "
                                   "X_TOTP_SECRET, or turn 2FA off on the "
                                   "capture account.")
                _fill_and_submit(page, _TOTP_INPUT, code)

            # 5) settle, then confirm
            for _ in range(6):
                if _signed_in(ctx):
                    break
                page.wait_for_timeout(2000)

            if not _signed_in(ctx):
                text = _page_text(page)
                if any(h in text for h in _BAD_CREDS_HINTS):
                    return False, ("X rejected the username or password. Check "
                                   "X_USERNAME / X_PASSWORD.")
                if any(h in text for h in _RATE_HINTS):
                    return False, ("X is rate-limiting or blocking this login. "
                                   "Wait, then try again.")
                return False, "Could not sign in to X (no auth cookie was set)."

            # Load home once so localStorage/origins settle before saving.
            try:
                page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
            except Exception:
                pass

            count = _write_state(ctx.storage_state())
            if not count:
                return False, "Signed in, but no X cookies could be saved."
            return True, f"Signed in to X and saved {count} cookie(s)."
        finally:
            try:
                browser.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def force_login():
    """Sign in now, regardless of what is on disk."""
    if not credentials_configured():
        return False, ("No X account configured. Set X_USERNAME and X_PASSWORD "
                       "(and X_EMAIL) in the environment / Space secrets.")
    with _lock:
        since = time.time() - _last_attempt["at"]
        if not _last_attempt["ok"] and since < _MIN_RETRY_SECONDS:
            return False, (f"Last sign-in failed {int(since)}s ago: "
                           f"{_last_attempt['message']} "
                           f"Waiting before retrying.")
        _last_attempt["at"] = time.time()
        try:
            ok, message = _do_login()
        except Exception as e:
            ok, message = False, f"Sign-in crashed: {e}"
        _last_attempt["ok"] = ok
        _last_attempt["message"] = message
        print(f"[x-login] {'ok' if ok else 'FAILED'} — {message}", flush=True)
        return ok, message


def ensure_session():
    """Make sure a usable X cookie exists, signing in only when needed.

    Safe to call before every job: it is a cheap file check unless the cookie is
    actually missing or about to expire. The session file lives on the server's
    own disk, so it survives restarts and only needs regenerating when X expires
    it.
    """
    if session_is_valid():
        return True, "Existing X session is valid."

    if not credentials_configured():
        return False, ("No saved X login and no X account configured — captures "
                       "will hit login walls.")
    return force_login()


def session_state() -> dict:
    """Everything the admin page needs. Never includes credentials."""
    state = _read_state()
    cookie = auth_cookie(state)
    expires = (cookie or {}).get("expires", 0)
    info = {
        "present": bool(state),
        "cookies": len(state.get("cookies", [])) if state else 0,
        "valid": session_is_valid(),
        "auto_login": credentials_configured(),
        "account": config.X_USERNAME or "",
        "has_2fa_secret": bool(config.X_TOTP_SECRET),
        "expires_in_days": (int((expires - time.time()) / 86400)
                            if expires and expires > 0 else None),
        "last_attempt": dict(_last_attempt),
        "path": str(config.X_STATE_FILE),
    }
    if state:
        try:
            info["modified"] = config.X_STATE_FILE.stat().st_mtime
        except OSError:
            info["modified"] = None
    return info


def warm_up_async() -> None:
    """Sign in at boot on a background thread, so the first report does not pay
    for it and the admin sees the result in the log immediately."""
    if not credentials_configured():
        return

    def run():
        try:
            ensure_session()
        except Exception as e:
            print(f"[x-login] warm-up failed: {e}", flush=True)

    threading.Thread(target=run, name="x-login-warmup", daemon=True).start()
