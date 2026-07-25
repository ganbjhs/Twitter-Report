"""X (Twitter) capture module.

Opens a post in the logged-in browser and takes ONE clean screenshot of the
tweet itself: header -> text -> media. Everything below the media — the
engagement/action bar (reply/repost/like/view/bookmark) and the aggregate
counts + "time · views" metadata line — is cropped out. Because we screenshot
the tweet `article` element (never the full page), the surrounding "your
account" UI is excluded too: the left nav rail, the right sidebar (trends /
who-to-follow) and the reply composer all live outside the article.

Returns:
    {"url", "status", "handle", "screenshot", "text"}
    status: "ok" | "login_wall" | "not_found" | "error: …"
"""
import random
import time
from pathlib import Path

try:                                    # src/ is on sys.path when the worker runs
    from shot_quality import screenshot_quality as _shot_quality
except Exception:                       # analyzer unavailable -> treat every shot as good
    def _shot_quality(path):
        return True, "no-analyzer"

_SHOOT_RETRIES = 2                       # extra in-capture retakes if a shot looks bad

TWEET_SELECTOR = 'article[data-testid="tweet"]'
LOGIN_WALL_HINTS = ['data-testid="loginButton"', "Sign in to X"]

# Genuine "this post really isn't there" states — safe to flag as not_found.
NOT_FOUND_PHRASES = [
    "this post is unavailable", "post unavailable", "this post was deleted",
    "hmm...this page doesn't exist", "doesn’t exist", "doesn't exist",
    "account doesn’t exist", "account doesn't exist",
    "has been suspended", "account suspended", "posts are protected",
    "no longer available",
]
# Transient X errors — the post is fine, X just fumbled the load. Reload & retry
# instead of falsely flagging not_found (the bug you hit).
TRANSIENT_PHRASES = ["something went wrong", "try reloading", "rate limit"]
_LOAD_ATTEMPTS = 3
_SELECTOR_TIMEOUT = 22000

# how close (px) a <time> metadata line must sit above the action bar to count
# as THIS tweet's metadata (and not a quoted tweet's timestamp far above).
_METADATA_LOOKBACK = 260
_TOP_PAD = 2          # keep a hair of breathing room at the crop edge
_MEDIA_TIMEOUT = 10000    # max ms to wait for the tweet's <img>s to fully decode
_IDLE_TIMEOUT = 3500      # short cap for network settle (X long-polls, never idles)

# JS: true once every <img> in the first tweet article has fully decoded (so we
# never screenshot a half-loaded post) and no spinner is still showing.
_ALL_MEDIA_READY = """() => {
  const art = document.querySelector('article[data-testid="tweet"]');
  if (!art) return false;
  const imgs = Array.from(art.querySelectorAll('img'));
  if (imgs.some(im => !im.complete || im.naturalWidth === 0)) return false;
  // any visible progress spinner means content is still coming in
  if (art.querySelector('[role="progressbar"], [aria-label="Loading"]')) return false;
  return true;
}"""


def _wait_rendered(page, tweet) -> None:
    """Block until the tweet's images are fully loaded (bounded, best-effort).

    Scrolls the tweet into view to trigger lazy loading, waits for the network
    to settle, then waits until every <img> in the tweet has decoded. Each step
    is time-boxed and swallows its own timeout, so a stubborn asset (e.g.
    deleted media) still falls through to a capture rather than hanging."""
    try:
        tweet.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    # Short network settle (X long-polls and never truly idles, so this is only
    # a nudge); the real gate is every <img> reporting fully decoded, which
    # returns as soon as the media is ready rather than burning the full budget.
    try:
        page.wait_for_load_state("networkidle", timeout=_IDLE_TIMEOUT)
    except Exception:
        pass
    try:
        page.wait_for_function(_ALL_MEDIA_READY, timeout=_MEDIA_TIMEOUT)
    except Exception:
        pass
    page.wait_for_timeout(500)   # brief settle for layout after the last image


def _visible_text(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _load_tweet(page, url: str) -> str:
    """Load the post and return 'ok' | 'login_wall' | 'not_found'.

    Retries transient X errors ("Something went wrong. Try reloading.") by
    reloading, so a working post is never falsely flagged not_found. Only the
    genuine unavailable-post phrases return not_found."""
    for attempt in range(_LOAD_ATTEMPTS):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            page.wait_for_timeout(1500)
            continue

        try:
            page.wait_for_selector(TWEET_SELECTOR, timeout=_SELECTOR_TIMEOUT)
            return "ok"
        except Exception:
            pass  # no tweet yet — figure out why

        txt = _visible_text(page)
        try:
            html = page.content()
        except Exception:
            html = ""

        if 'data-testid="loginButton"' in html or "sign in to x" in txt:
            return "login_wall"
        if any(p in txt for p in NOT_FOUND_PHRASES):
            return "not_found"
        if any(p in txt for p in TRANSIENT_PHRASES):
            # explicit transient error — click Retry if present, else reload
            try:
                page.get_by_role("button", name="Retry").first.click(timeout=1500)
                page.wait_for_selector(TWEET_SELECTOR, timeout=_SELECTOR_TIMEOUT)
                return "ok"
            except Exception:
                page.wait_for_timeout(1500)
                continue
        # unknown/slow state — give it one more reload before giving up
        page.wait_for_timeout(1500)
    return "not_found"


def _reveal_sensitive(page, tweet) -> None:
    """Click through X's sensitive-content gate so the media is visible in the
    shot. The gate uses a button labelled 'View' (whole-post warning) or 'Show'
    (per-media warning); both sit inside the tweet article."""
    for name in ("View", "Show"):
        try:
            btns = tweet.get_by_role("button", name=name, exact=True)
            for i in range(min(btns.count(), 4)):
                b = btns.nth(i)
                try:
                    if b.is_visible():
                        b.click(timeout=1500)
                        page.wait_for_timeout(600)
                except Exception:
                    pass
        except Exception:
            pass


def _read_handle(tweet) -> str:
    """The @handle from the tweet header, e.g. '@nasa'. Works even for
    /i/status/ links that hide the handle in the URL. '' if unreadable."""
    try:
        name = tweet.locator('[data-testid="User-Name"]').first.inner_text()
    except Exception:
        return ""
    for token in name.replace("\n", " ").split():
        if token.startswith("@"):
            return token
    return ""


def _crop_box(page, tweet):
    """Bounding box of the tweet clipped to end just above the engagement bar.

    Cut point = the highest of the main tweet's metadata `<time>` line and its
    action `[role="group"]`, so nothing engagement-related survives the crop.
    Falls back to the full article box if neither can be located."""
    box = tweet.bounding_box()
    if not box:
        return None
    art_top, art_bottom = box["y"], box["y"] + box["height"]

    def tops(selector):
        loc = tweet.locator(selector)
        found = []
        for i in range(min(loc.count(), 12)):
            try:
                b = loc.nth(i).bounding_box()
            except Exception:
                b = None
            if b and b["y"] > art_top + 60:   # skip anything in the header
                found.append((b["y"], b["y"] + b["height"]))
        return found

    groups = tops('[role="group"]')
    cut = min((t for t, _ in groups), default=art_bottom)

    # Pull the cut above the metadata/counts line if a <time> sits just above
    # the action bar (localized so a quoted tweet's timestamp is ignored).
    for t_top, t_bottom in tops("time"):
        if t_bottom <= cut + 4 and (cut - t_top) < _METADATA_LOOKBACK:
            cut = min(cut, t_top)

    height = max(cut - art_top - _TOP_PAD, 80)
    return {"x": box["x"], "y": art_top, "width": box["width"], "height": height}


def capture(page, url: str, shot_path: Path) -> dict:
    """Capture one X post. Returns a result dict; never raises for content issues."""
    result = {"url": url, "status": "ok", "handle": "", "screenshot": None, "text": ""}

    status = _load_tweet(page, url)
    if status != "ok":
        result["status"] = status
        try:
            page.screenshot(path=str(shot_path))   # evidence for debugging
            result["screenshot"] = str(shot_path)
        except Exception:
            pass
        return result

    tweet = page.locator(TWEET_SELECTOR).first

    # Remove any logged-out bottom banner / dialog that could overlap the tweet.
    for sel in ['[data-testid="BottomBar"]', '[role="dialog"] [aria-label="Close"]']:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                page.evaluate(
                    "(s)=>{const e=document.querySelector(s); if(e) e.remove();}", sel)
        except Exception:
            pass

    _reveal_sensitive(page, tweet)   # dismiss any "sensitive content" gate first
    _wait_rendered(page, tweet)      # don't shoot until the media has fully loaded
    result["handle"] = _read_handle(tweet)

    def _shoot():
        clip = _crop_box(page, tweet)
        if clip:
            page.screenshot(path=str(shot_path), clip=clip)
        else:                                   # last resort: whole article
            tweet.screenshot(path=str(shot_path))

    # Take the shot; if it comes out blank/black/half-loaded, give the post more
    # time (re-dismiss the sensitive gate, re-wait for media) and try again.
    _shoot()
    for _ in range(_SHOOT_RETRIES):
        good, _why = _shot_quality(str(shot_path))
        if good:
            break
        page.wait_for_timeout(2000)
        _reveal_sensitive(page, tweet)
        _wait_rendered(page, tweet)
        _shoot()
    result["screenshot"] = str(shot_path)

    try:
        result["text"] = tweet.locator('[data-testid="tweetText"]').first.inner_text()[:280]
    except Exception:
        pass

    time.sleep(random.uniform(1.0, 2.0))        # human-like pacing
    return result
