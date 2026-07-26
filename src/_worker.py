"""Worker body for parallel capture.

Kept in its own importable module so it pickles cleanly under the macOS 'spawn'
start method used by ProcessPoolExecutor. Each worker owns its own Playwright
browser and processes one chunk of links, so many X posts are screenshotted
concurrently instead of one-by-one.
"""
import sys
from pathlib import Path


def run_chunk(chunk, headless, storage_state, ctx_kwargs, src_path):
    sys.path.insert(0, src_path)
    from playwright.sync_api import sync_playwright
    import capture  # dispatcher
    from browser_backend import launch_browser  # local Chromium, or remote CDP

    results = []
    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        kwargs = dict(ctx_kwargs)
        if storage_state:
            kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**kwargs)
        page = ctx.new_page()
        for t in chunk:
            try:
                res = capture.capture(page, t["capture_url"], Path(t["shot"]), t["platform"])
            except Exception as e:  # network/timeout — flag, keep going
                res = {"url": t["capture_url"], "status": f"error: {e}",
                       "platform": t["platform"], "screenshot": None, "handle": ""}
            res.update({"idx": t["idx"], "category": t["category"],
                        "account_name": t["account"], "post_link": t["post_link"]})
            results.append(res)
        browser.close()
    return results
