"""Influencer capture — a PARALLEL implementation of the X capture.

`src/capture/x_capture.py` is frozen and untouched. This module deliberately
mirrors its structure (load/retry, sensitive-content gate, media-ready wait,
quality re-take) because that behaviour is proven, but differs in three ways
that define the Influencer report:

  1. THE CROP KEEPS ENGAGEMENT.  x_capture cuts at the *top* of the action bar
     so likes/reposts are excluded. Here the crop ends *below* the action bar,
     so display name, @handle, text, media and the visible likes/reposts are all
     inside one frame.
  2. IT PICKS THE RIGHT POST ON A REPLY PAGE.  On a reply URL the first
     `article` is the PARENT tweet, not the one you linked. We score every
     article on the page and choose the focused one.
  3. IT READS THE METRICS OFF THE PAGE.  Reactions/Comments/Reach/Shares are
     scraped for the metrics table in the document.

Returns:
    {"url", "status", "handle", "screenshot", "text", "metrics"}
    status : "ok" | "login_wall" | "not_found" | "error: …"
    metrics: {"reactions", "comments", "reach", "shares"} — display strings,
             "—" when a value could not be read.
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

TWEET_SELECTOR = 'article[data-testid="tweet"]'

NOT_FOUND_PHRASES = [
    "this post is unavailable", "post unavailable", "this post was deleted",
    "hmm...this page doesn't exist", "doesn’t exist", "doesn't exist",
    "account doesn’t exist", "account doesn't exist",
    "has been suspended", "account suspended", "posts are protected",
    "no longer available",
]
TRANSIENT_PHRASES = ["something went wrong", "try reloading", "rate limit"]

_LOAD_ATTEMPTS = 3
_SELECTOR_TIMEOUT = 22000
_MEDIA_TIMEOUT = 12000
_IDLE_TIMEOUT = 3500
_SHOOT_RETRIES = 2

_BOTTOM_PAD = 10      # breathing room below the engagement bar
_TOP_PAD = 2

MISSING = "—"

# --------------------------------------------------------------------------- #
# JS helpers
# --------------------------------------------------------------------------- #

# Ready when every image has decoded AND every video has either a painted first
# frame or a loaded poster — video/reel posts otherwise screenshot as a black
# rectangle, which is the main failure mode this report has to avoid.
_ALL_MEDIA_READY = """() => {
  const arts = document.querySelectorAll('article[data-testid="tweet"]');
  if (!arts.length) return false;
  for (const art of arts) {
    for (const im of art.querySelectorAll('img')) {
      if (!im.complete || im.naturalWidth === 0) return false;
    }
    for (const v of art.querySelectorAll('video')) {
      const posterReady = !v.poster || (v.poster && v.readyState >= 1);
      if (v.readyState < 2 && !posterReady) return false;
    }
    if (art.querySelector('[role="progressbar"], [aria-label="Loading"]')) return false;
  }
  return true;
}"""

# One pass over every article on the page, returning the signals we need to work
# out which one the URL actually points at.
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
      top: rect.top,
      height: rect.height,
    };
  });
}"""


def _status_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _visible_text(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _load_tweet(page, url: str) -> str:
    """Load the post; 'ok' | 'login_wall' | 'not_found'. Transient X errors are
    reloaded rather than mis-flagged as not_found."""
    for _ in range(_LOAD_ATTEMPTS):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            page.wait_for_timeout(1500)
            continue

        try:
            page.wait_for_selector(TWEET_SELECTOR, timeout=_SELECTOR_TIMEOUT)
            return "ok"
        except Exception:
            pass

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
            try:
                page.get_by_role("button", name="Retry").first.click(timeout=1500)
                page.wait_for_selector(TWEET_SELECTOR, timeout=_SELECTOR_TIMEOUT)
                return "ok"
            except Exception:
                page.wait_for_timeout(1500)
                continue
        page.wait_for_timeout(1500)
    return "not_found"


# --------------------------------------------------------------------------- #
# Choosing the right article (replies!)
# --------------------------------------------------------------------------- #
def _pick_article(page, url: str):
    """Return (locator, info) for the post the URL points at.

    On `x.com/<user>/status/<id>` where <id> is a REPLY, X renders the parent
    tweet(s) above it, so `article.first` is the wrong post. Articles are scored:
      +6  an anchor inside it points at this exact status id
      +3  no timestamp link in the name row (the focused tweet's signature)
      +2  its action bar reports view counts (only the focused tweet does)
      +1  it is the tallest article (the focused tweet is rendered larger)
    """
    try:
        infos = page.evaluate(_ARTICLE_INFO) or []
    except Exception:
        infos = []
    articles = page.locator(TWEET_SELECTOR)
    if not infos:
        return articles.first, {}
    if len(infos) == 1:
        return articles.first, infos[0]

    sid = _status_id(url)
    tallest = max(range(len(infos)), key=lambda i: infos[i].get("height") or 0)

    def score(i, info):
        s = 0
        if sid and any(f"/status/{sid}" in h for h in info.get("hrefs") or []):
            s += 6
        if not info.get("timeInName"):
            s += 3
        if "view" in (info.get("groupLabel") or "").lower():
            s += 2
        if i == tallest:
            s += 1
        return s

    best = max(range(len(infos)), key=lambda i: score(i, infos[i]))
    return articles.nth(best), infos[best]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _reveal_sensitive(page, tweet) -> None:
    """Click through X's sensitive-content gate so media is visible in the shot."""
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


def _wait_rendered(page, tweet) -> None:
    """Bounded, best-effort wait until images and video posters have painted."""
    try:
        tweet.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=_IDLE_TIMEOUT)
    except Exception:
        pass
    try:
        page.wait_for_function(_ALL_MEDIA_READY, timeout=_MEDIA_TIMEOUT)
    except Exception:
        pass
    page.wait_for_timeout(600)


def _read_handle(tweet) -> str:
    try:
        name = tweet.locator('[data-testid="User-Name"]').first.inner_text()
    except Exception:
        return ""
    for token in name.replace("\n", " ").split():
        if token.startswith("@"):
            return token
    return ""


# --------------------------------------------------------------------------- #
# Crop — KEEPS the engagement bar (the whole point of this report)
# --------------------------------------------------------------------------- #
def _crop_box(page, tweet):
    """Bounding box of the tweet ending just BELOW the engagement bar.

    x_capture cuts at the top of `[role="group"]`; we take that group's bottom
    edge instead, so likes and reposts stay in frame. Anything after the action
    bar (the reply composer, other replies) is still excluded because we clip to
    the article's own box.
    """
    box = tweet.bounding_box()
    if not box:
        return None
    art_top = box["y"]
    art_bottom = art_top + box["height"]

    bottoms = []
    try:
        groups = tweet.locator('[role="group"]')
        for i in range(min(groups.count(), 8)):
            try:
                b = groups.nth(i).bounding_box()
            except Exception:
                b = None
            if b and b["y"] > art_top + 40:   # skip anything up in the header
                bottoms.append(b["y"] + b["height"])
    except Exception:
        pass

    cut = max(bottoms) + _BOTTOM_PAD if bottoms else art_bottom
    cut = min(cut, art_bottom)                   # never spill past the article
    height = max(cut - art_top - _TOP_PAD, 80)
    return {"x": box["x"], "y": art_top, "width": box["width"], "height": height}


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
_NUM = r"([\d][\d,.  ]*\s*[KMB]?)"
_LABEL_PATTERNS = {
    "comments": re.compile(_NUM + r"\s*(?:replies|reply|comments|comment)\b", re.I),
    "shares":   re.compile(_NUM + r"\s*(?:reposts|repost|retweets|retweet)\b", re.I),
    "reactions": re.compile(_NUM + r"\s*(?:likes|like)\b", re.I),
    "reach":    re.compile(_NUM + r"\s*(?:views|view|impressions)\b", re.I),
}
_TESTIDS = {"comments": "reply", "shares": "retweet", "reactions": "like"}


def _to_int(raw: str):
    """'12,431' / '12.4K' / '1.2M' -> int. None when unparseable."""
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "").replace(" ", "").replace(" ", "")
    s = s.replace(",", "")
    mult = 1
    if s and s[-1] in "KkMmBb":
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[s[-1].lower()]
        s = s[:-1]
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


def compact(n) -> str:
    """X's own display style: 984, 1.2K, 45K, 1.2M."""
    if n is None:
        return MISSING
    n = int(n)
    if n < 1_000:
        return str(n)
    if n < 10_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    if n < 1_000_000:
        return f"{n // 1_000}K"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{n // 1_000_000}M"


def _from_label(text: str) -> dict:
    """Pull every metric we can out of one aria-label string."""
    found = {}
    for key, pattern in _LABEL_PATTERNS.items():
        m = pattern.search(text or "")
        if m:
            value = _to_int(m.group(1))
            if value is not None:
                found[key] = value
    return found


def read_metrics(tweet) -> dict:
    """Reactions / Comments / Reach / Shares for one post.

    Source order, most reliable first:
      1. the action bar's aria-label — X writes the exact counts there, e.g.
         "12 replies, 3 reposts, 45 likes, 6 bookmarks, 7890 views";
      2. each button's own aria-label / title;
      3. the button's visible text (already abbreviated, e.g. "1.2K");
      4. for Reach only, the analytics link or the "… Views" metadata line.
    Anything still unknown stays None and renders as "—".
    """
    values = {"reactions": None, "comments": None, "reach": None, "shares": None}

    # 1) the whole action bar in one attribute
    try:
        groups = tweet.locator('[role="group"]')
        for i in range(min(groups.count(), 6)):
            label = groups.nth(i).get_attribute("aria-label") or ""
            for key, value in _from_label(label).items():
                if values.get(key) is None:
                    values[key] = value
            if all(values[k] is not None for k in ("reactions", "comments", "shares")):
                break
    except Exception:
        pass

    # 2+3) per-button aria-label / title / visible text
    for key, testid in _TESTIDS.items():
        if values[key] is not None:
            continue
        for sel in (f'[data-testid="{testid}"]',
                    f'[data-testid="un{testid}"]'):      # liked/reposted variants
            try:
                btn = tweet.locator(sel).first
                if not btn.count():
                    continue
                for attr in ("aria-label", "title"):
                    got = _from_label(btn.get_attribute(attr) or "")
                    if key in got:
                        values[key] = got[key]
                        break
                if values[key] is None:
                    values[key] = _to_int((btn.inner_text() or "").strip())
            except Exception:
                continue
            if values[key] is not None:
                break

    # 4) Reach — analytics link, then the "… Views" metadata line
    if values["reach"] is None:
        for sel in ('a[href$="/analytics"]', '[data-testid="analyticsButton"]'):
            try:
                el = tweet.locator(sel).first
                if not el.count():
                    continue
                for attr in ("aria-label", "title"):
                    got = _from_label(el.get_attribute(attr) or "")
                    if "reach" in got:
                        values["reach"] = got["reach"]
                        break
                if values["reach"] is None:
                    values["reach"] = _to_int((el.inner_text() or "").split()[0])
            except Exception:
                continue
            if values["reach"] is not None:
                break
    if values["reach"] is None:
        try:
            got = _from_label(tweet.inner_text() or "")
            if "reach" in got:
                values["reach"] = got["reach"]
        except Exception:
            pass

    return {
        "reactions": compact(values["reactions"]),
        "comments": compact(values["comments"]),
        "reach": compact(values["reach"]),
        "shares": compact(values["shares"]),
        "_raw": {k: v for k, v in values.items()},
    }


# --------------------------------------------------------------------------- #
# Followers — read from the author's profile, not the post
# --------------------------------------------------------------------------- #
_FOLLOWER_SELECTORS = ('a[href$="/verified_followers"]', 'a[href$="/followers"]')
_PROFILE_READY = '[data-testid="UserName"], a[href$="/verified_followers"]'


def read_followers(page, handle: str) -> str:
    """The author's follower count, e.g. '171K'. '—' when unavailable.

    Follower count lives on the profile, not on the post, so this costs one
    extra page load. Callers cache it per handle (see inf_worker) so a report
    with 40 posts from 5 accounts pays for 5 visits, not 40.

    NOTE: this navigates the page away from the post — only call it after the
    screenshot and the post metrics have been taken.
    """
    h = (handle or "").strip().lstrip("@")
    if not h:
        return MISSING
    try:
        page.goto(f"https://x.com/{h}", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector(_PROFILE_READY, timeout=15000)
    except Exception:
        return MISSING

    for sel in _FOLLOWER_SELECTORS:
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            text = (loc.inner_text() or "").strip()
            if not text:
                continue
            m = re.search(r"([\d][\d,.\s]*[KMB]?)", text.replace("\n", " "))
            value = _to_int(m.group(1)) if m else None
            if value is not None:
                return compact(value)
        except Exception:
            continue
    return MISSING


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
def capture(page, url: str, shot_path: Path) -> dict:
    """Capture one X post with engagement in frame. Never raises for content."""
    result = {"url": url, "status": "ok", "handle": "", "screenshot": None,
              "text": "", "platform": "x",
              "metrics": {"followers": MISSING, "reactions": MISSING,
                          "comments": MISSING, "reach": MISSING,
                          "shares": MISSING}}

    status = _load_tweet(page, url)
    if status != "ok":
        result["status"] = status
        try:
            page.screenshot(path=str(shot_path))     # evidence for debugging
            result["screenshot"] = str(shot_path)
        except Exception:
            pass
        return result

    # Strip overlays that could sit on top of the post.
    for sel in ('[data-testid="BottomBar"]', '[role="dialog"] [aria-label="Close"]'):
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                page.evaluate(
                    "(s)=>{const e=document.querySelector(s); if(e) e.remove();}", sel)
        except Exception:
            pass

    tweet, _info = _pick_article(page, url)

    _reveal_sensitive(page, tweet)
    _wait_rendered(page, tweet)
    result["handle"] = _read_handle(tweet)

    def _shoot():
        clip = _crop_box(page, tweet)
        if clip:
            page.screenshot(path=str(shot_path), clip=clip)
        else:
            tweet.screenshot(path=str(shot_path))

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
        result["metrics"] = read_metrics(tweet)
    except Exception:
        pass

    try:
        result["text"] = tweet.locator(
            '[data-testid="tweetText"]').first.inner_text()[:280]
    except Exception:
        pass

    time.sleep(random.uniform(1.0, 2.0))            # human-like pacing
    return result
