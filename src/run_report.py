"""Capture runner (X-only): read a list of X/Twitter post links, screenshot each
one in the logged-in browser, verify the shots, write results.json.

Capture runs in PARALLEL across worker processes, so many links are
screenshotted concurrently instead of one-by-one.

Source resolution (first match wins):
    1. a CLI argument: an .xlsx path, or "-" to paste links on stdin
    2. config/links.xlsx       (default)

Usage:
    python src/run_report.py links.xlsx
    python src/run_report.py -                      # paste links, Ctrl-D
    python src/run_report.py --workers 6            # more parallelism
    python src/run_report.py --headed               # watch the browser
    python src/run_report.py --keep-engagement      # keep the like/views line

By default the shot is cropped above the engagement bar. `--keep-engagement`
keeps it — on a comment link that means the parent's like/views line AND the
comment's own, both in frame.

The X login comes from sessions/x_state.json — create it once with:
    python src/save_sessions.py x
"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import input_loader  # noqa: E402
import shot_quality  # noqa: E402s
import _worker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = str(Path(__file__).resolve().parent)
OUT = ROOT / "reports"
SHOTS = OUT / "screenshots"
DEFAULT_WORKERS = 3
MIN_SHOT_BYTES = 1024   # a valid PNG screenshot is comfortably larger than this

CTX_KWARGS = {
    "viewport": {"width": 1280, "height": 1600},
    "locale": "en-IN",
    "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}


def _arg_value(argv, flag, default):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def resolve_source(argv) -> str:
    skip = set()
    if "--workers" in argv:
        skip.add(argv.index("--workers") + 1)
    for j, a in enumerate(argv[1:], 1):
        if j in skip or a.startswith("--"):
            continue
        return a   # the first positional arg is the source (an .xlsx path or "-")
    return str(ROOT / "config" / "links.xlsx")


def x_storage_state():
    """Load only sessions/x_state.json — this project is X-only."""
    f = ROOT / "sessions" / "x_state.json"
    if not f.exists():
        print("[runner] NO saved X session — running logged-out "
              "(run: python src/save_sessions.py x)")
        return None
    try:
        d = json.loads(f.read_text())
    except Exception:
        print("[runner] x_state.json unreadable — running logged-out")
        return None
    print("[runner] loaded X session")
    return {"cookies": d.get("cookies", []), "origins": d.get("origins", [])}


def build_tasks(rows, keep_engagement: bool = False) -> list:
    tasks = []
    for i, row in enumerate(rows, 1):
        capture_url = (row.get("link") or "").strip()
        if not capture_url:
            continue
        post_link = (row.get("post_link") or "").strip() or capture_url
        account = row.get("account_name") or f"tweet_{i}"
        safe = "".join(c if c.isalnum() else "_" for c in account)[:40]
        shot = SHOTS / f"{i:02d}_{safe}.png"
        tasks.append({
            "idx": i, "capture_url": capture_url, "post_link": post_link,
            "account": account, "platform": "x",
            "category": row.get("category", "Uncategorized"),
            "shot": str(shot),
            "keep_engagement": keep_engagement,
        })
    return tasks


def _shot_ok(result) -> bool:
    if result.get("status") != "ok":
        return False
    shot = result.get("screenshot")
    return bool(shot) and Path(shot).exists() and Path(shot).stat().st_size > MIN_SHOT_BYTES


def _why_poor(result):
    """Why this shot is not trustworthy, or None when it is.

    Three independent checks, strongest evidence first. The first two are facts
    the capture observed in the DOM — a dialog that was still painted over the
    post, and a frame that did not reach both the parent and the reply it
    promised. The third is the pixel analyzer, which is the backstop for
    anything the DOM checks did not anticipate.
    """
    if result.get("overlay"):
        return "an X dialog was still covering the post"
    if result.get("frame_ok") is False:
        return "the frame did not cover the whole post + reply"
    good, why = shot_quality.screenshot_quality(result["screenshot"])
    return None if good else why


def _quality_ok(result) -> bool:
    """A shot that exists, isn't blank/black/half-loaded, isn't covered by a
    dialog, and actually framed what the crop promised."""
    if not _shot_ok(result):
        return False
    return _why_poor(result) is None


def verify(collected) -> None:
    """Report which links produced a usable screenshot and which did not."""
    ok = [r for r in collected if _shot_ok(r)]
    bad = [r for r in collected if not _shot_ok(r)]
    print(f"[verify] {len(ok)}/{len(collected)} links produced a clean screenshot")
    for r in bad:
        why = r.get("status") if r.get("status") != "ok" else "empty/missing screenshot"
        print(f"[verify]   ✗ {r.get('account_name')}  ({why})  {r.get('post_link')}")


def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    argv = sys.argv
    headless = "--headed" not in argv
    workers = int(_arg_value(argv, "--workers", DEFAULT_WORKERS))
    keep_engagement = "--keep-engagement" in argv

    rows = input_loader.load(resolve_source(argv))
    tasks = build_tasks(rows, keep_engagement)
    print(f"[runner] {len(tasks)} X link(s) loaded")
    if keep_engagement:
        print("[runner] keeping the engagement line (likes / views) in frame")
    if not tasks:
        print("[runner] nothing to capture"); return

    state = x_storage_state()
    workers = max(1, min(workers, len(tasks)))

    chunks = [[] for _ in range(workers)]
    for n, t in enumerate(tasks):
        chunks[n % workers].append(t)
    chunks = [c for c in chunks if c]
    print(f"[runner] capturing with {len(chunks)} parallel worker(s)...")

    collected = []
    if len(chunks) == 1:
        collected = _worker.run_chunk(chunks[0], headless, state, CTX_KWARGS, SRC)
    else:
        with ProcessPoolExecutor(max_workers=len(chunks)) as ex:
            futures = [ex.submit(_worker.run_chunk, c, headless, state, CTX_KWARGS, SRC)
                       for c in chunks]
            for fut in futures:
                collected.extend(fut.result())

    # retry pass: re-attempt anything without a clean screenshot once,
    # sequentially (recovers transient timeouts from heavy parallelism).
    by_idx = {r["idx"]: r for r in collected}
    failed_idx = {r["idx"] for r in collected if not _shot_ok(r)}
    if failed_idx:
        retry_tasks = [t for t in tasks if t["idx"] in failed_idx]
        print(f"[runner] retrying {len(retry_tasks)} link(s) sequentially...")
        recovered = 0
        for r in _worker.run_chunk(retry_tasks, headless, state, CTX_KWARGS, SRC):
            if _shot_ok(r) or not _shot_ok(by_idx.get(r["idx"], {})):
                if _shot_ok(r) and not _shot_ok(by_idx.get(r["idx"], {})):
                    recovered += 1
                by_idx[r["idx"]] = r
        print(f"[runner] retry recovered {recovered}/{len(retry_tasks)}")

    # quality pass: recapture any 'ok' shot that came out blank / black /
    # half-loaded / still behind a sensitive gate — give it a fresh, slower try.
    poor_idx = {i for i, r in by_idx.items() if _shot_ok(r) and not _quality_ok(r)}
    if poor_idx:
        poor_tasks = [t for t in tasks if t["idx"] in poor_idx]
        print(f"[quality] recapturing {len(poor_tasks)} low-quality screenshot(s)...")
        for t in poor_tasks:
            why = _why_poor(by_idx[t["idx"]]) or "unknown"
            print(f"[quality]   ↻ {by_idx[t['idx']].get('account_name')}  ({why})")
        # recapture overwrites the same file, so always take the fresh attempt
        # and count how many now pass the quality check.
        fixed = 0
        for r in _worker.run_chunk(poor_tasks, headless, state, CTX_KWARGS, SRC):
            by_idx[r["idx"]] = r
            if _quality_ok(r):
                fixed += 1
        print(f"[quality] improved {fixed}/{len(poor_tasks)}")

    # Final gate. A dialog that survived every retake is a FACT, not a guess —
    # the screenshot demonstrably shows a popup instead of the post, so the link
    # is reported rather than printed into the document. The pixel-based
    # judgements deliberately do NOT demote: they are heuristics, and a
    # half-dark screenshot of the right post still beats a missing page.
    blocked = [r for r in by_idx.values() if r.get("status") == "ok" and r.get("overlay")]
    for r in blocked:
        r["status"] = "overlay_blocked"
    if blocked:
        print(f"[quality] dropping {len(blocked)} shot(s) still covered by an X dialog")
    cropped = [r for r in by_idx.values()
               if r.get("status") == "ok" and r.get("frame_ok") is False]
    if cropped:
        print(f"[quality] {len(cropped)} shot(s) may be missing the parent post "
              "or the reply")
        for r in cropped:
            print(f"[quality]   ! {r.get('account_name')}  {r.get('post_link')}")

    collected = list(by_idx.values())
    collected.sort(key=lambda r: r.get("idx", 0))
    for r in collected:
        r.pop("idx", None)
        print(f"  [x] {r['status']:12} {r.get('handle') or ''}  {r['account_name']}")

    verify(collected)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(collected, indent=2))
    print(f"[runner] wrote {OUT / 'results.json'}")


if __name__ == "__main__":
    main()
