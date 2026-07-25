"""X (Twitter) constants + login helpers, shared by save_sessions.py, the
capture dispatcher, and run_report.py.

This project is X-only. One place declares which domains X cookies live on,
where to log in, the cookie that proves a live login, and how to recognize a
logout wall.
"""

PLATFORMS = {
    "x": {
        "domains": ("x.com", "twitter.com"),
        "login_url": "https://x.com/login",
        "home_url": "https://x.com/home",
        "auth_cookie": "auth_token",          # present only when logged in
        "login_hints": ('data-testid="loginButton"', "Sign in to X", "Log in"),
    },
}

DEFAULT_PLATFORM = "x"


def platform_of(url: str) -> str:
    """Every link in this project is an X/Twitter post — always route to X."""
    return DEFAULT_PLATFORM


def domain_matches(value: str, platform: str = DEFAULT_PLATFORM) -> bool:
    return any(d in (value or "") for d in PLATFORMS[platform]["domains"])
