"""Take X's overlay layers off the post before a screenshot is taken.

WHY THIS EXISTS
---------------
Both capture modules used to do this, once, right after the page loaded:

    for sel in ('[data-testid="BottomBar"]', '[role="dialog"] [aria-label="Close"]'):
        page.evaluate("(s)=>{const e=document.querySelector(s); if(e) e.remove()}", sel)

For the second selector that removes the dialog's **close button** and leaves
the dialog itself sitting on top of the tweet, so it lands in the pixels. And it
ran too early: X's "Confirm age in X mobile app" sheet only opens *after* the
sensitive-content gate's "View" button is clicked, which happens later.

The second-order damage is worse than the visible one. While a modal is open X
locks the page (`overflow:hidden` / `position:fixed` on `<body>`), and
`window.scrollBy` — how the capture aligns a parent tweet to the top of the
viewport — becomes a silent no-op. The frame then starts at the wrong Y and the
reply gets cut off the bottom. A stray dialog and a mis-framed reply are the
same bug.

USAGE

    info = overlays.dismiss(page)      # polite close, then remove, then unlock
    if overlays.present(page): ...     # still covered — do not trust this shot
    if info["age_gated"]: ...          # X wants mobile-app age verification

Call `dismiss` again before every retake: X re-renders the column as media
settles and can bring a sheet back.
"""

# Every layer X paints over the column. `mask` is the dim backdrop; leaving it
# behind is what washes a screenshot out even after the dialog itself is gone.
_LAYERS = (
    '[role="dialog"]',
    '[data-testid="sheetDialog"]',
    '[data-testid="confirmationSheetDialog"]',
    '[data-testid="mask"]',
    '[data-testid="twc-cc-mask"]',
    '[data-testid="BottomBar"]',
    '[data-testid="ScrollSnap-List"] [role="dialog"]',
)

# Buttons that close a sheet politely, in the order worth trying. A real click
# lets X restore its own scroll lock; ripping the node out does not.
_CLOSE_LABELS = ("Close", "Not now", "Dismiss", "Cancel", "Maybe later", "Skip")

# Phrases that mean "X will not show this content in a desktop browser". Since
# July 2026 sensitive media can demand age verification through the mobile app,
# which no amount of clicking here can satisfy — the post has to be reported as
# uncapturable rather than shipped as a grey placeholder.
_AGE_PHRASES = (
    "confirm your age", "confirm age", "age-restricted", "age restricted",
    "verify your age", "age verification",
)

_TWEET = 'article[data-testid="tweet"]'

# JS: remove every overlay layer, return the text that was on them (so the
# caller can tell an age gate from an ordinary nag), then release the scroll
# lock. Text is collected BEFORE removal — afterwards there is nothing to read.
_STRIP = """(sels) => {
  const out = {removed: 0, texts: []};
  const kill = (el) => {
    if (!el || !el.parentNode) return;
    const t = (el.innerText || '').trim();
    if (t) out.texts.push(t.slice(0, 400));
    el.remove();
    out.removed++;
  };

  for (const sel of sels) {
    for (const el of Array.from(document.querySelectorAll(sel))) kill(el);
  }

  // Anything else pinned over the whole viewport. Bounded hard — it must start
  // at the top-left corner and cover almost everything — so the left nav rail
  // (~275px wide) and the post column can never match.
  const vw = innerWidth, vh = innerHeight;
  for (const el of Array.from(document.body.querySelectorAll('div'))) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed') continue;
    if (el.querySelector('article[data-testid="tweet"]')) continue;
    const r = el.getBoundingClientRect();
    if (r.top > 2 || r.left > 2) continue;
    if (r.width < vw * 0.85 || r.height < vh * 0.85) continue;
    kill(el);
  }

  // Release the scroll lock a modal leaves behind, whether or not we removed
  // anything: window.scrollBy is a no-op while it is in place, and that breaks
  // the framing far more quietly than a visible dialog does.
  for (const el of [document.documentElement, document.body]) {
    if (!el) continue;
    el.style.overflow = 'visible';
    el.style.position = 'static';
    el.style.top = '';
    el.style.height = '';
  }
  return out;
}"""

# JS: is any overlay layer currently painted over the column? Tiny leftovers
# (X keeps zero-sized dialog stubs around) do not count.
_PRESENT = """(sels) => {
  for (const sel of sels) {
    for (const el of Array.from(document.querySelectorAll(sel))) {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.width > 60 && r.height > 60) return true;
    }
  }
  return false;
}"""


def present(page) -> bool:
    """True when a dialog / sheet / backdrop is still over the post."""
    try:
        return bool(page.evaluate(_PRESENT, list(_LAYERS)))
    except Exception:
        return False


def _click_close(page) -> bool:
    """Close the top dialog through its own button, so X unwinds its state."""
    try:
        dialog = page.locator('[role="dialog"]').locator("visible=true").first
        if not dialog.is_visible(timeout=600):
            return False
    except Exception:
        return False

    for label in _CLOSE_LABELS:
        for finder in (lambda: dialog.get_by_label(label, exact=True),
                       lambda: dialog.get_by_role("button", name=label, exact=True)):
            try:
                btn = finder().locator("visible=true").first
                btn.click(timeout=1000)
                page.wait_for_timeout(350)
                return True
            except Exception:
                continue
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(350)
        return True
    except Exception:
        return False


def dismiss(page) -> dict:
    """Clear every overlay off the post.

    Returns {"removed": int, "age_gated": bool, "still_open": bool}. `age_gated`
    means one of the layers was X's age-verification wall, which is a permanent
    "no" on desktop — the caller should report the post rather than screenshot a
    placeholder.
    """
    info = {"removed": 0, "age_gated": False, "still_open": False}

    # Polite first — a real close lets X restore the scroll position itself.
    for _ in range(2):
        if not present(page):
            break
        if not _click_close(page):
            break

    try:
        res = page.evaluate(_STRIP, list(_LAYERS)) or {}
    except Exception:
        info["still_open"] = present(page)
        return info

    info["removed"] = int(res.get("removed") or 0)
    blob = " ".join(res.get("texts") or []).lower()
    info["age_gated"] = any(p in blob for p in _AGE_PHRASES)
    info["still_open"] = present(page)
    return info


# JS: hide the "Hide" toggle X paints over media that was just revealed through
# the sensitive-content gate. It sits INSIDE the article and on top of the
# image, so the crop cannot exclude it — it ends up as a stray button floating
# over the picture in the finished report. Matched on its exact label so an
# ordinary post, which has no such button, is never touched.
_HIDE_REVEAL_TOGGLE = """() => {
  let n = 0;
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    for (const b of art.querySelectorAll('button, [role="button"]')) {
      if ((b.innerText || '').trim() !== 'Hide') continue;
      b.style.display = 'none';
      n++;
    }
  }
  return n;
}"""


def hide_media_controls(page) -> None:
    """Take X's post-reveal 'Hide' button off the media (best effort)."""
    try:
        page.evaluate(_HIDE_REVEAL_TOGGLE)
    except Exception:
        pass


def article_age_gated(article) -> bool:
    """True when the post itself carries X's age-restriction notice.

    The notice outlives the dialog: dismissing the sheet leaves a grey "Age-
    restricted content" panel where the media should be, so the dialog check
    alone would pass a screenshot that shows nothing.

    The post's own text is subtracted before matching, so a tweet that merely
    *talks about* age-restricted content is not mistaken for a gated one.
    """
    try:
        whole = article.inner_text() or ""
    except Exception:
        return False
    try:
        body = article.locator('[data-testid="tweetText"]').first.inner_text() or ""
    except Exception:
        body = ""
    chrome = whole.replace(body, " ").lower() if body else whole.lower()
    return any(p in chrome for p in _AGE_PHRASES)
