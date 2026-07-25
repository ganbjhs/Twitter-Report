"""Authentication: username + password from `.env`, signed session cookie,
CSRF tokens, and login rate limiting.

No OAuth, no external identity provider — self-contained, as specified.
Credentials never leave the server; the browser only ever holds an opaque
signed session cookie.
"""
import hmac
import secrets

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from . import config
from .jobs import store


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def _check_password(stored: str, given: str) -> bool:
    """Constant-time compare. A stored value beginning with `$2` is a bcrypt
    hash; anything else is a plaintext password from .env."""
    if stored.startswith("$2"):
        try:
            import bcrypt
            return bcrypt.checkpw(given.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    return hmac.compare_digest(stored, given)


def verify_credentials(username: str, password: str):
    """Return the canonical username on success, else None.

    Always runs a comparison, even for an unknown user, so response timing
    doesn't reveal which usernames exist.
    """
    username = (username or "").strip()
    stored = config.USERS.get(username)
    ok = _check_password(stored, password or "") if stored else _check_password(
        "$2b$12$" + "x" * 53, password or "")
    return username if (stored and ok) else None


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def login_session(request: Request, username: str) -> None:
    request.session.clear()
    request.session["user"] = username
    request.session["csrf"] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request):
    return request.session.get("user")


def require_user(request: Request) -> str:
    """FastAPI dependency for HTML pages — redirects to /login when signed out."""
    user = current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="login required",
            headers={"Location": f"/login?next={request.url.path}"})
    return user


def require_user_api(request: Request) -> str:
    """FastAPI dependency for JSON endpoints — 401 instead of a redirect."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def redirect_to_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("csrf") or ""
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=400,
                            detail="This form expired. Reload the page and try again.")


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def client_ip(request: Request) -> str:
    """Client IP, honouring X-Forwarded-For (we sit behind Caddy / Cloud Run)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_blocked(ip: str) -> bool:
    return store.recent_login_failures(ip) >= config.LOGIN_MAX_ATTEMPTS


def note_login_failure(ip: str) -> None:
    store.record_login_failure(ip)


def note_login_success(ip: str) -> None:
    store.clear_login_failures(ip)
