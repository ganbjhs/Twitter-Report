"""Where the browser comes from: a local Chromium, or a remote one over CDP.

NEW FILE — nothing existing was rewritten. It lives in `src/` only so the
capture workers can `import browser_backend` after they put `src/` on sys.path;
it is not part of the frozen X capture logic.

Why it exists: the free, no-credit-card app hosts cap RAM at ~512 MB, which
cannot run Chromium. Offloading the browser to a remote service (Browserless)
keeps the app small enough to fit, while every line of capture logic — open URL,
reveal sensitive content, crop, screenshot, read metrics — stays exactly the
same, because it all operates on a Playwright `page`.

    BROWSER_BACKEND=local        (default) p.chromium.launch(...)
    BROWSER_BACKEND=browserless  p.chromium.connect_over_cdp(BROWSERLESS_WS)

With BROWSER_BACKEND unset this returns precisely what the callers used before,
so the tested local/CLI behaviour is unchanged.
"""
import os

DEFAULT_BACKEND = "local"
_CONNECT_TIMEOUT_MS = 60_000


def backend() -> str:
    return (os.environ.get("BROWSER_BACKEND", "") or DEFAULT_BACKEND).strip().lower()


def endpoint() -> str:
    return (os.environ.get("BROWSERLESS_WS", "") or "").strip()


def is_remote() -> bool:
    return backend() == "browserless" and bool(endpoint())


def describe() -> str:
    """Human-readable, with the token stripped — safe to print in logs."""
    if not is_remote():
        return "local Chromium"
    url = endpoint().split("?")[0]
    return f"remote browser via CDP ({url})"


class _RemoteBrowser:
    """Thin proxy over a CDP-connected browser.

    Playwright's `connect_over_cdp` returns a Browser that *usually* supports
    `new_context()`, but some CDP endpoints only expose the default context. The
    proxy tries the normal path and falls back to the existing context with the
    cookies applied by hand, so callers can keep using `browser.new_context(...)`
    unchanged either way.
    """

    def __init__(self, browser):
        self._browser = browser

    def new_context(self, **kwargs):
        try:
            return self._browser.new_context(**kwargs)
        except Exception:
            contexts = self._browser.contexts
            ctx = contexts[0] if contexts else self._browser.new_context()
            state = kwargs.get("storage_state") or {}
            cookies = state.get("cookies") if isinstance(state, dict) else None
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                except Exception:
                    pass
            return ctx

    def close(self):
        # Closing the CDP connection ends the remote session, which is what
        # stops the meter running on a metered browser service.
        try:
            self._browser.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._browser, name)


def launch_browser(playwright, headless: bool = True, **launch_kwargs):
    """Return a Browser, local or remote depending on the environment."""
    if is_remote():
        remote = playwright.chromium.connect_over_cdp(
            endpoint(), timeout=_CONNECT_TIMEOUT_MS)
        return _RemoteBrowser(remote)
    return playwright.chromium.launch(headless=headless, **launch_kwargs)
