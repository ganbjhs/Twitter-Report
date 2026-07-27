"""X (Twitter) capture module.

Opens a post in the logged-in browser and takes ONE clean screenshot of the
tweet itself: header -> text -> media. Everything below the media — the
engagement/action bar (reply/repost/like/view/bookmark) and the aggregate
counts + "time · views" metadata line — is cropped out. Because we clip to the
tweet `article` element (never the full page), the surrounding "your account" UI
is excluded too: the left nav rail, the right sidebar (trends / who-to-follow)
and the reply composer all live outside the article.

WHEN THE LINK IS A REPLY the shot covers the conversation, not just the reply:
X renders the parent above the reply, so the frame runs

    parent name/@handle + text + media  ->  reply text + media

as one continuous image. The parent's own action bar (which would otherwise sit
in the middle of that frame) is hidden before the shot, so no likes/reposts/
replies appear anywhere in the picture.

Returns:
    {"url", "status", "handle", "screenshot", "text"}
    status: "ok" | "login_wall" | "not_found" | "error: …"
"""
import random
import re
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

# How many ancestor posts to keep above a reply. 1 = "the parent", which is what
# the report asks for; raising it walks further up a long thread and makes a
# correspondingly taller image.
_THREAD_ANCESTORS = 1
_ALIGN_PAD = 10           # px left above the first article after scrolling it up

# JS: true once every <img> in the captured articles (parent + reply, or just the
# one post) has fully decoded — so we never screenshot a half-loaded post — and
# no spinner is still showing. Scoped to the range we actually shoot, so other
# people's replies further down the page can never hold the capture up.
_ALL_MEDIA_READY = """([lo, hi]) => {
  const arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  const scope = arts.slice(lo, hi + 1);
  if (!scope.length) return false;
  for (const art of scope) {
    const imgs = Array.from(art.querySelectorAll('img'));
    if (imgs.some(im => !im.complete || im.naturalWidth === 0)) return false;
    // any visible progress spinner means content is still coming in
    if (art.querySelector('[role="progressbar"], [aria-label="Loading"]')) return false;
  }
  return true;
}"""

# JS: one pass over every article, returning the signals needed to work out which
# one the URL actually points at (see _pick_article).
_ARTICLE_INFO = """() => {
  const arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  return arts.map((a, i) => {
    const userName = a.querySelector('[data-testid="User-Name"]');
    const group = a.querySelector('[role="group"]');
    const hrefs = Array.from(a.querySelectorAll('a[href*="/status/"]'))
                       .map(x => x.getAttribute('href') || '');
    const rect = a.getBoundingClientRect();
    return {
      index: i,
      // Ancestor tweets in a thread show a timestamp link inside the name row;
      // the focused tweet does not (its date sits in a metadata line below).
      timeInName: !!(userName && userName.querySelector('time')),
      groupLabel: group ? (group.getAttribute('aria-label') || '') : '',
      hrefs: hrefs,
      height: rect.height,
    };
  });
}"""

# JS: hide the action bars of the ancestor posts we are keeping in frame. Their
# bars sit *between* the parent's media and the reply, so clipping cannot remove
# them — they have to come out of the layout. Measure everything first, then
# hide, so collapsing one bar cannot shift another out from under the test.
_HIDE_ENGAGEMENT = """([lo, hi]) => {
  const arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  const doomed = [];
  for (const art of arts.slice(lo, hi + 1)) {
    const top = art.getBoundingClientRect().top;
    for (const g of art.querySelectorAll('[role="group"]')) {
      if (g.getBoundingClientRect().top > top + 60) doomed.push(g);   // not the header
    }
  }
  doomed.forEach(g => { g.style.display = 'none'; });
  return doomed.length;
}"""


# JS: drop X's sticky "← Post" bar out of the column. It floats over whatever is
# at the top of the viewport, so once we scroll a parent tweet up to the top edge
# it paints straight over that tweet's name row. Nothing here is inside the
# frame we want, and the page is thrown away after the shot.
_HIDE_STICKY_CHROME = """() => {
  const col = document.querySelector('[data-testid="primaryColumn"]');
  if (!col) return 0;
  let n = 0;
  for (const el of col.querySelectorAll('div')) {
    const pos = getComputedStyle(el).position;
    if (pos !== 'sticky' && pos !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    // only the bar pinned at the top, and never a wrapper holding the posts
    if (r.height > 0 && r.top < 200 && !el.querySelector('article[data-testid="tweet"]')) {
      el.style.display = 'none';
      n++;
    }
  }
  return n;
}"""


def _status_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else ""


_FOCUSED_TIMEOUT = 8000

# JS: position of an article among all of them, so the ancestor above it can be
# addressed. -1 once a re-render has swapped the node out.
_ARTICLE_INDEX = """el => Array.from(
    document.querySelectorAll('article[data-testid="tweet"]')).indexOf(el)"""


def _locate_focused(page, url: str):
    """(locator, index) of the post the URL names, or (None, -1).

    Only that post's own article links to its status id (its timestamp, photo
    and analytics links all carry it), so this identifies it outright — no
    inference, and the locator re-resolves on every use, which the index alone
    does not survive. It is scrolled into view first because X unmounts articles
    that are off-screen, and an unmounted post cannot be found at all."""
    sid = _status_id(url)
    if not sid:
        return None, -1
    loc = page.locator(f'{TWEET_SELECTOR}:has(a[href*="/status/{sid}"])').first
    try:
        loc.wait_for(state="attached", timeout=_FOCUSED_TIMEOUT)
        loc.scroll_into_view_if_needed(timeout=3000)
        idx = loc.evaluate(_ARTICLE_INDEX)
    except Exception:
        return None, -1
    return (loc, idx) if isinstance(idx, int) and idx >= 0 else (None, -1)


_PICK_ATTEMPTS = 3       # rescans while the column is still settling
_PICK_BACKOFF = 900      # ms between rescans


def _owns_status(info, sid: str) -> bool:
    """True when this article links to the status id the URL names — the one
    signal that identifies the focused post outright rather than by inference."""
    return bool(sid) and any(f"/status/{sid}" in h for h in info.get("hrefs") or [])


def _best_article(infos, sid: str) -> int:
    """Index of the most likely focused article. Scoring:
      +6  an anchor inside it points at this exact status id
      +3  no timestamp link in the name row (the focused tweet's signature)
      +2  its action bar reports view counts (only the focused tweet does)
      +1  it is the tallest article (the focused tweet is rendered larger)
    Ties fall to the earliest article, so a plain post page still picks 0.
    """
    tallest = max(range(len(infos)), key=lambda i: infos[i].get("height") or 0)

    def score(i, info):
        s = 6 if _owns_status(info, sid) else 0
        if not info.get("timeInName"):
            s += 3
        if "view" in (info.get("groupLabel") or "").lower():
            s += 2
        if i == tallest:
            s += 1
        return s

    return max(range(len(infos)), key=lambda i: score(i, infos[i]))


def _pick_article(page, url: str):
    """Return (index, count) of the article the URL points at.

    On `x.com/<user>/status/<id>` where <id> is a REPLY, X renders the parent
    tweet(s) above it, so article 0 is the wrong post — the shot would be of
    somebody else's tweet.

    A pick is only trusted once the winner actually links to the URL's status
    id. Anything weaker means the column was still rendering (or a virtualised
    re-render swapped the nodes mid-scan), which lands one article too high and
    silently screenshots the parent alone; rescan instead. The best guess is
    returned once the attempts run out."""
    sid = _status_id(url)
    fallback = (0, 1)
    for attempt in range(_PICK_ATTEMPTS):
        try:
            infos = page.evaluate(_ARTICLE_INFO) or []
        except Exception:                       # mid-render — try again
            infos = []
        if infos:
            idx = _best_article(infos, sid)
            fallback = (idx, len(infos))
            if not sid or _owns_status(infos[idx], sid):
                return idx, len(infos)
        if attempt < _PICK_ATTEMPTS - 1:
            page.wait_for_timeout(_PICK_BACKOFF)
    return fallback


def _hide_ancestor_engagement(page, lo: int, hi: int) -> None:
    """Drop the parent posts' like/repost/reply bars out of the layout."""
    if lo >= hi:
        return
    try:
        page.evaluate(_HIDE_ENGAGEMENT, [lo, hi - 1])
    except Exception:
        pass


def _hide_sticky_chrome(page) -> None:
    try:
        page.evaluate(_HIDE_STICKY_CHROME)
    except Exception:
        pass


def _align_top(page, locator) -> None:
    """Scroll so the first captured article sits just under the viewport top.

    Gives the clip the best chance of fitting in one viewport-sized frame (a
    parent + reply is roughly twice as tall as a single post) and keeps the clip
    coordinates positive."""
    try:
        locator.evaluate(
            "(el, pad) => window.scrollBy(0, el.getBoundingClientRect().top - pad)",
            _ALIGN_PAD)
    except Exception:
        return
    page.wait_for_timeout(250)


def _wait_rendered(page, tweet, lo: int = 0, hi: int = 0) -> None:
    """Block until the captured articles' images are loaded (bounded, best-effort).

    Scrolls the tweet into view to trigger lazy loading, waits for the network
    to settle, then waits until every <img> in articles [lo, hi] has decoded.
    Each step is time-boxed and swallows its own timeout, so a stubborn asset
    (e.g. deleted media) still falls through to a capture rather than hanging."""
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
        page.wait_for_function(_ALL_MEDIA_READY, arg=[lo, hi], timeout=_MEDIA_TIMEOUT)
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


def _crop_box(page, tweet, top_el=None):
    """Bounding box ending just above the focused tweet's engagement bar.

    Cut point = the highest of the main tweet's metadata `<time>` line and its
    action `[role="group"]`, so nothing engagement-related survives the crop.
    Falls back to the full article box if neither can be located.

    `top_el` is where the frame *starts*: the focused tweet itself for a normal
    post, or the parent article for a reply — which is what makes the shot cover
    parent + reply in one image."""
    box = tweet.bounding_box()
    if not box:
        return None
    art_top, art_bottom = box["y"], box["y"] + box["height"]

    frame_top, frame_x, frame_w = art_top, box["x"], box["width"]
    if top_el is not None:
        try:
            top_box = top_el.bounding_box()
        except Exception:
            top_box = None
        if top_box and top_box["y"] < art_top:
            frame_top, frame_x = top_box["y"], top_box["x"]
            frame_w = max(frame_w, top_box["width"])

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

    height = max(cut - frame_top - _TOP_PAD, 80)
    return {"x": frame_x, "y": frame_top, "width": frame_w, "height": height}


def _screenshot_clip(page, clip, shot_path) -> None:
    """Save `clip` (viewport coordinates, as bounding_box reports them).

    A parent + reply frame is often taller than the viewport, and clipping past
    the viewport edge fails. When that happens, switch to a full-page capture and
    translate the clip into document coordinates, which is the space full-page
    screenshots clip in."""
    view = page.viewport_size or {}
    view_h = view.get("height") or 0
    if view_h and clip["y"] >= 0 and clip["y"] + clip["height"] <= view_h:
        page.screenshot(path=str(shot_path), clip=clip)
        return
    try:
        sx, sy = page.evaluate("() => [window.scrollX, window.scrollY]")
    except Exception:
        sx, sy = 0, 0
    doc_clip = dict(clip, x=clip["x"] + sx, y=clip["y"] + sy)
    page.screenshot(path=str(shot_path), clip=doc_clip, full_page=True)


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

    # Remove any logged-out bottom banner / dialog that could overlap the tweet.
    for sel in ['[data-testid="BottomBar"]', '[role="dialog"] [aria-label="Close"]']:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                page.evaluate(
                    "(s)=>{const e=document.querySelector(s); if(e) e.remove();}", sel)
        except Exception:
            pass

    articles = page.locator(TWEET_SELECTOR)
    tweet, idx = _locate_focused(page, url)     # article 0 is the PARENT on a reply
    if tweet is None:                           # no usable id — fall back to scoring
        idx, _count = _pick_article(page, url)
        tweet = articles.nth(idx)
    first = max(0, idx - _THREAD_ANCESTORS)     # == idx unless this post is a reply
    top_el = articles.nth(first) if first < idx else None

    # A sensitive-content gate on the parent would blank half the frame, so clear
    # the gate on every post we are about to shoot, not just the linked one.
    for i in range(first, idx + 1):
        _reveal_sensitive(page, articles.nth(i))
    _wait_rendered(page, tweet, first, idx)     # don't shoot until media has loaded
    result["handle"] = _read_handle(tweet)

    def _shoot():
        # Re-hide each time: X re-renders the column as media settles, which can
        # bring a parent's action bar back.
        _hide_ancestor_engagement(page, first, idx)
        if top_el is not None:
            _hide_sticky_chrome(page)      # or the "← Post" bar covers the parent
            _align_top(page, top_el)
        clip = _crop_box(page, tweet, top_el)
        try:
            if clip:
                _screenshot_clip(page, clip, shot_path)
            else:                               # last resort: whole article
                tweet.screenshot(path=str(shot_path))
        except Exception:
            # A clip that lands outside the frame must not cost us the post —
            # fall back to the post on its own (engagement bar and all).
            tweet.screenshot(path=str(shot_path))

    # Take the shot; if it comes out blank/black/half-loaded, give the post more
    # time (re-dismiss the sensitive gate, re-wait for media) and try again.
    _shoot()
    for _ in range(_SHOOT_RETRIES):
        good, _why = _shot_quality(str(shot_path))
        if good:
            break
        page.wait_for_timeout(2000)
        for i in range(first, idx + 1):
            _reveal_sensitive(page, articles.nth(i))
        _wait_rendered(page, tweet, first, idx)
        _shoot()
    result["screenshot"] = str(shot_path)

    try:
        result["text"] = tweet.locator('[data-testid="tweetText"]').first.inner_text()[:280]
    except Exception:
        pass

    time.sleep(random.uniform(1.0, 2.0))        # human-like pacing
    return result
