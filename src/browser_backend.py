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


# X switches its left navigation from icons to icons+labels at a 1280 px
# breakpoint. Sitting exactly ON that breakpoint is unsafe: a remote browser
# reserves room for a scrollbar, so the usable width lands just under 1280 while
# JS still renders the labels — the nav then paints ON TOP of the tweet column
# and ends up inside the article's bounding box, so it lands in the screenshot.
# Anything comfortably past the breakpoint lays out correctly.
MIN_REMOTE_VIEWPORT_WIDTH = 1500


def _widen_viewport(kwargs: dict) -> dict:
    viewport = kwargs.get("viewport")
    if not isinstance(viewport, dict):
        return kwargs
    width = viewport.get("width") or 0
    if width >= MIN_REMOTE_VIEWPORT_WIDTH:
        return kwargs
    kwargs = dict(kwargs)
    kwargs["viewport"] = {**viewport, "width": MIN_REMOTE_VIEWPORT_WIDTH}
    return kwargs


class _RemoteContext:
    """Wraps a CDP browser context so every page really gets the viewport.

    `new_context(viewport=…)` is not honoured over a CDP connection: Playwright
    reports the size you asked for, but `window.innerWidth` stays at the remote
    service's default (800 px was observed). X then lays out in its narrow mode,
    paints the nav over the tweet column, and the article's bounding box is
    computed in that wrong geometry — so the screenshot contains the nav and
    clips the tweet. Forcing the size on the page issues the emulation override
    that actually takes effect.
    """

    def __init__(self, context, viewport):
        self._context = context
        self._viewport = viewport

    def new_page(self):
        page = self._context.new_page()
        if self._viewport:
            try:
                page.set_viewport_size(self._viewport)
            except Exception:
                pass
        return page

    def __getattr__(self, name):
        return getattr(self._context, name)


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
        kwargs = _widen_viewport(kwargs)
        viewport = kwargs.get("viewport")
        try:
            ctx = self._browser.new_context(**kwargs)
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
        return _RemoteContext(ctx, viewport)

    def close(self):
        # Closing the CDP connection ends the remote session, which is what
        # stops the meter running on a metered browser service.
        try:
            self._browser.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._browser, name)


def _remote_endpoint_with_window() -> str:
    """Ask the remote service to launch its browser at a usable window size.

    Emulating a viewport is not enough on a CDP connection: the real browser
    window stays whatever the service launched, and X measures the *window* when
    it lays out. If that window is small, X renders the nav labels over the tweet
    column, and the nav then sits inside the article's box and lands in the
    screenshot. Browserless accepts a `launch` query parameter for this.
    """
    import json
    import urllib.parse

    url = endpoint()

    # Use the CHROME build, not Chromium. The default endpoint serves the
    # open-source Chromium, which has no H.264/AAC — and X's videos are H.264,
    # so every video post renders as "The media could not be played" instead of
    # a poster frame. Real Chrome ships those codecs.
    head, sep, query = url.partition("?")
    if not urllib.parse.urlparse(head).path.strip("/"):
        head = head.rstrip("/") + "/chrome"
        url = head + sep + query

    if "launch=" in url:
        return url                      # caller configured it themselves
    # `defaultViewport` is the one that actually counts: the service applies its
    # own (800x600 was observed) and that wins over both `new_context(viewport=)`
    # and `page.set_viewport_size()`, leaving window.innerWidth at 800 no matter
    # what Playwright reports.
    launch = urllib.parse.quote(json.dumps({
        "args": [f"--window-size={MIN_REMOTE_VIEWPORT_WIDTH},1600"],
        "defaultViewport": {"width": MIN_REMOTE_VIEWPORT_WIDTH, "height": 1600},
    }))
    return f"{url}{'&' if '?' in url else '?'}launch={launch}"


def launch_browser(playwright, headless: bool = True, **launch_kwargs):
    """Return a Browser, local or remote depending on the environment."""
    if is_remote():
        remote = playwright.chromium.connect_over_cdp(
            _remote_endpoint_with_window(), timeout=_CONNECT_TIMEOUT_MS)
        return _RemoteBrowser(remote)
    return playwright.chromium.launch(headless=headless, **launch_kwargs)
