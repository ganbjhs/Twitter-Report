"""Worker body for parallel Influencer capture.

A deliberate parallel of `src/_worker.py`, which stays frozen: that worker
imports the X capture dispatcher, this one imports `inf_capture` directly. No
shared file needed a routing change (prompt §3.3, option (a)).

Kept importable at module level so it pickles cleanly under the 'spawn' start
method used by ProcessPoolExecutor on macOS.
"""
import sys
from pathlib import Path


def run_chunk(chunk, headless, storage_state, ctx_kwargs, src_path, inf_path):
    # src_path gives the worker `shot_quality`; inf_path gives it `inf_capture`.
    for p in (src_path, inf_path):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    from playwright.sync_api import sync_playwright
    from browser_backend import launch_browser   # local Chromium, or remote CDP
    import inf_capture

    results = []
    # Follower counts live on the profile, so each one costs an extra page load.
    # Cached per handle for the life of this worker: 40 posts from 5 accounts
    # means 5 profile visits, not 40.
    followers_cache = {}

    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        kwargs = dict(ctx_kwargs)
        if storage_state:
            kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**kwargs)
        page = ctx.new_page()
        for t in chunk:
            try:
                res = inf_capture.capture(page, t["capture_url"], Path(t["shot"]))
            except Exception as e:      # network/timeout — flag it, keep going
                res = {"url": t["capture_url"], "status": f"error: {e}",
                       "platform": "x", "screenshot": None, "handle": "",
                       "metrics": {"followers": "—", "reactions": "—",
                                   "comments": "—", "reach": "—", "shares": "—"}}

            # Followers last: reading it navigates away from the post, so the
            # screenshot and the post metrics must already be captured.
            handle = (res.get("handle") or "").strip().lower()
            if res.get("status") == "ok" and handle:
                if handle not in followers_cache:
                    try:
                        followers_cache[handle] = inf_capture.read_followers(
                            page, handle)
                    except Exception:
                        followers_cache[handle] = "—"
                res.setdefault("metrics", {})["followers"] = followers_cache[handle]

            res.update({"idx": t["idx"], "category": t["category"],
                        "account_name": t["account"], "post_link": t["post_link"]})
            results.append(res)
        browser.close()
    return results
