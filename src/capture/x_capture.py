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
replies appear anywhere in the picture. The frame is checked against that
promise before the shot is accepted (`_frame_covers`), so a reply that came out
cropped to its parent is retaken instead of shipped.

`keep_engagement=True` asks for the opposite crop, for reports that are ABOUT
the numbers: the cut moves to just below the focused post's action bar and the
ancestors keep theirs, so a reply comes out as

    parent name/@handle + text + media + like/views  ->  reply text + media
    + like/views

Nothing else changes — same framing, same retakes, same result dict — and the
default is the tight crop above, so a caller that does not ask for this gets
exactly the picture it always got.

Overlays — X's dialogs, sheets and dim backdrops — are cleared by `overlays`
before every take. See that module for why a stray dialog and a mis-framed
reply are the same bug.

Returns:
    {"url", "status", "handle", "screenshot", "text", "overlay", "frame_ok"}
    status : "ok" | "login_wall" | "not_found" | "age_restricted" | "error: …"
    overlay: True when a dialog was STILL over the post when the shot was taken
    frame_ok: False when the frame did not cover what the crop promised
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

try:
    import overlays
except Exception:                       # never let a missing helper kill a capture
    class overlays:                     # noqa: N801 — stand-in, same call surface
        @staticmethod
        def dismiss(page):
            return {"removed": 0, "age_gated": False, "still_open": False}

        @staticmethod
        def present(page):
            return False

        @staticmethod
        def hide_media_controls(page):
            pass

        @staticmethod
        def article_age_gated(article):
            return False

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
_BOTTOM_PAD = 10      # breathing room below the action bar when it is KEPT
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


# JS: does this article carry X's "Replying to @someone" line? That line is the
# post's own statement that it is a reply, so it holds even when the status id
# could not be read and the article had to be chosen by scoring.
_IS_REPLY = """el => /(^|\\n)\\s*Replying to/.test(el.innerText || '')"""

# JS: the top of the nearest thing painted BELOW the action bar, or null when the
# bar is the last thing in the post. X puts its "Relevant people" block INSIDE
# the article, a couple of px under the bar, so the article's own bottom edge is
# useless as a limit — clamping to it still lets a line of that block into the
# frame. This gives the real floor.
_NEXT_BELOW = """(el, barBottom) => {
  let top = Infinity;
  for (const n of el.querySelectorAll('*')) {
    const r = n.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.top >= barBottom - 1) {
      top = Math.min(top, r.top);
    }
  }
  return Number.isFinite(top) ? top : null;
}"""


def _floor_below(tweet, bar_bottom: float, fallback: float) -> float:
    """How far below the action bar the crop may reach without clipping the
    next block. Falls back to the article's bottom when nothing follows."""
    try:
        top = tweet.evaluate(_NEXT_BELOW, bar_bottom)
    except Exception:
        top = None
    return min(fallback, top) if top is not None else fallback


def _is_reply(locator) -> bool:
    try:
        return bool(locator.evaluate(_IS_REPLY))
    except Exception:
        return False


def _ensure_parent(page, url: str, tweet, idx: int):
    """Re-resolve a reply that ended up with no ancestor above it.

    X virtualises the conversation column, so scrolling the reply into view can
    unmount the parent — which lands the reply at index 0. `first` then equals
    `idx`, the frame silently shrinks to the reply alone, and (when the pick was
    one article off) to the parent alone. Both are the bug this guards.

    Scrolling back to the top of the column remounts the ancestors; the focused
    post is then located again by status id. Returns the original pair when
    nothing better can be found, so a genuinely parentless post still captures.
    """
    if idx > 0 or not _is_reply(tweet):
        return tweet, idx
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(600)
    except Exception:
        return tweet, idx
    again, again_idx = _locate_focused(page, url)
    if again is not None and again_idx > 0:
        return again, again_idx
    return tweet, idx


def _expand_text(page, article) -> None:
    """Click a post's 'Show more' so its full text is inside the frame.

    X truncates long posts; on a parent that means the report shows half a
    sentence. Only the in-place expander is used — the same label also exists as
    a *link* elsewhere on the page, and clicking that navigates away — and the
    URL is checked afterwards so a surprise navigation is undone rather than
    silently capturing a different page.
    """
    before = page.url
    for finder in (lambda: article.get_by_test_id("tweet-text-show-more-link"),
                   lambda: article.get_by_role("button", name="Show more",
                                               exact=True)):
        try:
            btn = finder().locator("visible=true").first
            if btn.count() == 0:
                continue
            btn.click(timeout=1200)
            page.wait_for_timeout(400)
        except Exception:
            continue
        if page.url != before:              # it was a link after all — undo it
            try:
                page.go_back(wait_until="domcontentloaded", timeout=20000)
                page.wait_for_selector(TWEET_SELECTOR, timeout=_SELECTOR_TIMEOUT)
            except Exception:
                pass
            return
        return


def _frame_covers(clip, tweet, top_el) -> bool:
    """Does `clip` actually contain what the crop promised?

    Two ways the promise breaks, both of which used to ship silently:
      * the frame starts BELOW the parent's top edge — the parent's name row is
        cut off, or the parent is missing entirely
      * the frame ends before the reply's own text — the picture is the parent
        with the reply sheared off the bottom (the reported failure)
    """
    if not clip:
        return False
    bottom = clip["y"] + clip["height"]

    if top_el is not None:
        try:
            top_box = top_el.bounding_box()
        except Exception:
            top_box = None
        if not top_box or clip["y"] > top_box["y"] + 4:
            return False

    try:
        art = tweet.bounding_box()
    except Exception:
        art = None
    if art and bottom < art["y"] + 60:      # the focused post barely made it in
        return False

    try:
        body = tweet.locator('[data-testid="tweetText"]').first.bounding_box()
    except Exception:
        body = None
    if body and body["y"] + body["height"] > bottom + 2:
        return False                        # its text runs past the cut
    return True


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


def _crop_box(page, tweet, top_el=None, keep_engagement=False):
    """Bounding box ending just above the focused tweet's engagement bar.

    Cut point = the highest of the main tweet's metadata `<time>` line and its
    action `[role="group"]`, so nothing engagement-related survives the crop.
    Falls back to the full article box if neither can be located.

    With `keep_engagement` the cut goes the other way — to the BOTTOM of that
    same action bar — so the "time · views" line and the reply/repost/like
    counts stay in the picture. The bar's own bottom is the anchor rather than
    the article's, because the article also contains the reply composer and the
    thread below it.

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
    if keep_engagement:
        # Below the bar. The metadata/counts line needs no special handling —
        # it sits above the bar, so it is already inside the frame.
        bar_bottom = max((b for _, b in groups), default=art_bottom)
        # The pad is a courtesy, not a promise: give it up rather than clip the
        # "Relevant people" block X paints a few px under the bar.
        cut = min(bar_bottom + _BOTTOM_PAD,
                  _floor_below(tweet, bar_bottom, art_bottom))
        cut = max(cut, bar_bottom)           # never eat into the bar itself
    else:
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


def capture(page, url: str, shot_path: Path, keep_engagement: bool = False) -> dict:
    """Capture one X post. Returns a result dict; never raises for content issues.

    `keep_engagement` keeps every captured post's like/views line in the frame
    (see the module docstring); the default crops them all out."""
    result = {"url": url, "status": "ok", "handle": "", "screenshot": None,
              "text": "", "overlay": False, "frame_ok": True}

    status = _load_tweet(page, url)
    if status != "ok":
        result["status"] = status
        try:
            page.screenshot(path=str(shot_path))   # evidence for debugging
            result["screenshot"] = str(shot_path)
        except Exception:
            pass
        return result

    # Clear X's dialogs / sheets / dim backdrop and release the scroll lock they
    # leave behind — `_align_top` below is a no-op while that lock is in place.
    age_gated = overlays.dismiss(page)["age_gated"]

    articles = page.locator(TWEET_SELECTOR)
    tweet, idx = _locate_focused(page, url)     # article 0 is the PARENT on a reply
    if tweet is None:                           # no usable id — fall back to scoring
        idx, _count = _pick_article(page, url)
        tweet = articles.nth(idx)
    tweet, idx = _ensure_parent(page, url, tweet, idx)   # a reply must have its parent
    first = max(0, idx - _THREAD_ANCESTORS)     # == idx unless this post is a reply
    top_el = articles.nth(first) if first < idx else None

    # A sensitive-content gate on the parent would blank half the frame, so clear
    # the gate on every post we are about to shoot, not just the linked one — and
    # expand any truncated text, so the parent goes in whole rather than as half
    # a sentence ending in "Show more".
    for i in range(first, idx + 1):
        _reveal_sensitive(page, articles.nth(i))
        _expand_text(page, articles.nth(i))
    # "View" on age-restricted media opens X's mobile-app verification sheet, so
    # the gate has to be re-checked AFTER revealing, not only on load.
    age_gated = overlays.dismiss(page)["age_gated"] or age_gated
    age_gated = age_gated or any(overlays.article_age_gated(articles.nth(i))
                                 for i in range(first, idx + 1))

    _wait_rendered(page, tweet, first, idx)     # don't shoot until media has loaded
    result["handle"] = _read_handle(tweet)

    def _shoot():
        # Re-dismiss and re-hide each time: X re-renders the column as media
        # settles, which brings back both a parent's action bar and any sheet.
        overlays.dismiss(page)
        overlays.hide_media_controls(page)   # the "Hide" toggle sits ON the media
        if not keep_engagement:
            # Kept, the parent's bar is the whole point of the wider crop; the
            # frame is then parent + its counts -> reply + its counts.
            _hide_ancestor_engagement(page, first, idx)
        if top_el is not None:
            _hide_sticky_chrome(page)      # or the "← Post" bar covers the parent
            _align_top(page, top_el)
        clip = _crop_box(page, tweet, top_el, keep_engagement)
        covers = _frame_covers(clip, tweet, top_el)
        try:
            if clip:
                _screenshot_clip(page, clip, shot_path)
            else:                               # last resort: whole article
                tweet.screenshot(path=str(shot_path))
                covers = top_el is None         # an element shot cannot hold both
        except Exception:
            # A clip that lands outside the frame must not cost us the post —
            # fall back to the post on its own (engagement bar and all).
            tweet.screenshot(path=str(shot_path))
            covers = top_el is None
        # Whatever was still painted over the post IS in the pixels (rule 16).
        return covers, overlays.present(page)

    # Take the shot; if it comes out blank/black/half-loaded, still covered by a
    # dialog, or framed to less than the crop promised, give the post more time
    # (re-dismiss the sensitive gate, re-wait for media) and try again.
    covers, covered = _shoot()
    for _ in range(_SHOOT_RETRIES):
        good, _why = _shot_quality(str(shot_path))
        if good and covers and not covered:
            break
        page.wait_for_timeout(2000)
        for i in range(first, idx + 1):
            _reveal_sensitive(page, articles.nth(i))
        _wait_rendered(page, tweet, first, idx)
        covers, covered = _shoot()
    result["screenshot"] = str(shot_path)
    result["frame_ok"] = bool(covers)
    result["overlay"] = bool(covered)
    if age_gated:
        # Desktop cannot satisfy X's age check, so retrying is pointless and the
        # picture is a grey placeholder. Report it instead of shipping it.
        result["status"] = "age_restricted"

    try:
        result["text"] = tweet.locator('[data-testid="tweetText"]').first.inner_text()[:280]
    except Exception:
        pass

    time.sleep(random.uniform(1.0, 2.0))        # human-like pacing
    return result
