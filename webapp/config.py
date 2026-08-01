"""Configuration — everything comes from the environment / `.env`.

Secrets (app logins, session key) live in `.env`, which is gitignored and never
reaches the browser. See `.env.example` for the full documented list.
"""
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# .env loading (tiny parser — avoids another dependency)
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path = None) -> None:
    """Populate os.environ from a KEY=VALUE file. Real env vars always win, so
    `docker run -e FOO=bar` overrides the file."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


load_dotenv()


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name, default=False):
    return (os.environ.get(name, "") or str(int(default))).strip().lower() in (
        "1", "true", "yes", "on")


def _path(name, default):
    p = Path(os.environ.get(name, "") or default)
    return p if p.is_absolute() else (ROOT / p).resolve()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def _parse_users(raw: str) -> dict:
    """'alice:pw1,bob:$2b$12$…' -> {'alice': 'pw1', 'bob': '$2b$12$…'}

    A value beginning with $2 is treated as a bcrypt hash; anything else is a
    plaintext password compared in constant time.
    """
    users = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, pw = pair.partition(":")
        name, pw = name.strip(), pw.strip()
        if name and pw:
            users[name] = pw
    return users


def _load_users() -> dict:
    """APP_USERS is the canonical form: 'alice:pw1,bob:pw2'.

    APP_USER + APP_PASS is also accepted for a single account, because
    `gcloud run deploy --set-env-vars` splits on commas — so a multi-user
    APP_USERS needs the awkward '^@^' delimiter syntax, and a one-account deploy
    is much easier to get right with two plain variables.
    """
    users = _parse_users(os.environ.get("APP_USERS", ""))
    single_user = os.environ.get("APP_USER", "").strip()
    single_pass = os.environ.get("APP_PASS", "").strip()
    if single_user and single_pass:
        users.setdefault(single_user, single_pass)
    return users


USERS = _load_users()

# --------------------------------------------------------------------------- #
# Shared X capture account — used to sign in headlessly and regenerate
# sessions/x_state.json when it is missing (free hosts have an ephemeral disk).
# Use a dedicated / throwaway account, never a personal one.
# --------------------------------------------------------------------------- #
X_USERNAME = os.environ.get("X_USERNAME", "").strip()
X_PASSWORD = os.environ.get("X_PASSWORD", "").strip()
X_EMAIL = os.environ.get("X_EMAIL", "").strip()        # for X's "confirm email" step
X_TOTP_SECRET = os.environ.get("X_TOTP_SECRET", "").strip()   # only if 2FA is on

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
SESSION_HOURS = _int("SESSION_HOURS", 12)
COOKIE_SECURE = _bool("COOKIE_SECURE", False)

# --------------------------------------------------------------------------- #
# Capture performance
#
# Chromium runs on this server, so the limit is the box's RAM and cores, not a
# third party's plan. Rule of thumb: one worker per 1–1.5 GB of free RAM
# (a browser costs ~0.5–1 GB).
#
# `WORKERS` is the name used in the deployment docs; CAPTURE_WORKERS is accepted
# as an alias so older .env files keep working.
# --------------------------------------------------------------------------- #
MAX_CONCURRENT_JOBS = max(1, _int("MAX_CONCURRENT_JOBS", 1))
CAPTURE_WORKERS = max(1, _int("WORKERS", _int("CAPTURE_WORKERS", 3)))

# Ceiling for the per-job "Capture speed" picker on the submit form. A user can
# choose any value up to this; the server clamps anything above it.
#
# Keep it honest about the hardware. Browsers only run in parallel if there are
# CORES to run them on — Chromium spends the capture decoding images and video,
# so on a 1-vCPU box three browsers take turns on one core instead of running
# side by side, and the wall-clock barely moves while the RAM cost is real.
# Raise this when you add vCPUs, not before. RAM is the second limit:
# MAX_CONCURRENT_JOBS x MAX_WORKERS browsers at ~0.5-1 GB each.
MAX_WORKERS = max(CAPTURE_WORKERS, _int("MAX_WORKERS", 4))

# The Influencer report defaults to one browser: it looks up each author's
# follower count once and caches it, but that cache lives in the worker PROCESS,
# so a second worker re-fetches the same profiles. Raise it if you have the
# cores and the accounts rarely repeat.
INFLUENCER_WORKERS = max(1, _int("INFLUENCER_WORKERS", 1))

JOB_TIMEOUT_MINUTES = max(1, _int("JOB_TIMEOUT_MINUTES", 90))

# Upload limits
MAX_UPLOAD_MB = max(1, _int("MAX_UPLOAD_MB", 5))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_LINKS = max(1, _int("MAX_LINKS", 200))

# Retention
RETENTION_DAYS = max(1, _int("RETENTION_DAYS", 7))
MAX_DATA_GB = max(1, _int("MAX_DATA_GB", 5))

# queue (always-on host) | inline (scale-to-zero host)
EXECUTION_MODE = (os.environ.get("EXECUTION_MODE", "queue") or "queue").strip().lower()

# Paths
DATA_DIR = _path("DATA_DIR", "data")
SESSIONS_DIR = _path("SESSIONS_DIR", "sessions")
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "jobs.db"
X_STATE_FILE = SESSIONS_DIR / "x_state.json"

PORT = _int("PORT", 8000)

# Login rate limiting
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_WINDOW_MINUTES = _int("LOGIN_WINDOW_MINUTES", 15)

REPORT_TYPES = ("twitter", "influencer")


def startup_warnings() -> list:
    """Misconfiguration the admin should see in the log at boot."""
    warn = []
    if not USERS:
        warn.append("No app logins configured — nobody can log in. Set "
                    "APP_USERS=admin:somepassword (or APP_USER + APP_PASS).")
    if EXECUTION_MODE == "inline" and JOB_TIMEOUT_MINUTES >= 60:
        warn.append(f"JOB_TIMEOUT_MINUTES={JOB_TIMEOUT_MINUTES} but a "
                    "scale-to-zero host caps a request at 60 minutes — the "
                    "host will cut the job off first. Use 50 or less.")
    if any(pw in ("change-me-now", "changeme", "password") for pw in USERS.values()):
        warn.append("APP_USERS still contains a default password — change it.")
    if not SESSION_SECRET or SESSION_SECRET.startswith("change-me"):
        warn.append("SESSION_SECRET is unset or default — sessions will not "
                    "survive a restart and are not secure. Generate one with "
                    "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"`.")
    if not X_STATE_FILE.exists() and not (X_USERNAME and X_PASSWORD):
        warn.append(f"No X login cookie at {X_STATE_FILE} and no X_USERNAME / "
                    "X_PASSWORD to sign in with — captures will hit login "
                    "walls. Either set those in .env, or run "
                    "`python save_login.py x` on your own machine and copy the "
                    "file onto the server.")
    if X_TOTP_SECRET and not (X_USERNAME and X_PASSWORD):
        warn.append("X_TOTP_SECRET is set but X_USERNAME / X_PASSWORD are not — "
                    "auto sign-in is disabled.")
    if EXECUTION_MODE not in ("queue", "inline"):
        warn.append(f"EXECUTION_MODE={EXECUTION_MODE!r} is not 'queue' or "
                    "'inline' — falling back to 'queue'.")
    return warn


def effective_session_secret() -> str:
    """The real secret, or an ephemeral one so dev still works (with a warning
    already emitted by startup_warnings)."""
    return SESSION_SECRET or secrets.token_urlsafe(48)


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
