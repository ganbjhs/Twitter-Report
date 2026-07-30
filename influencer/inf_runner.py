"""Influencer capture runner — the parallel of `src/run_report.py`.

Same proven shape (parallel workers, a sequential retry pass, a quality
re-capture pass, results.json), pointed at `inf_worker` / `inf_capture` instead
of the frozen X path. `src/run_report.py` is not imported or modified.

Usage (normally invoked by run_influencer.py):
    python influencer/inf_runner.py links.xlsx --workers 4 --headed

The X login comes from sessions/x_state.json, exactly as the X report does —
metrics are only reliably visible when logged in.
"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
INF = str(Path(__file__).resolve().parent)

sys.path.insert(0, SRC)
sys.path.insert(0, INF)
import input_loader   # noqa: E402  — frozen, imported read-only
import shot_quality   # noqa: E402  — frozen, imported read-only
import inf_worker     # noqa: E402

OUT = ROOT / "reports"
SHOTS = OUT / "screenshots"
DEFAULT_WORKERS = 3
MIN_SHOT_BYTES = 1024

# A taller viewport than the X report uses: the Influencer crop reaches all the
# way past the engagement bar, so the whole post must fit on screen to clip it.
CTX_KWARGS = {
    "viewport": {"width": 1280, "height": 2000},
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
        return a
    return str(ROOT / "config" / "links.xlsx")


def x_storage_state():
    f = ROOT / "sessions" / "x_state.json"
    if not f.exists():
        print("[runner] NO saved X session — running logged-out "
              "(metrics will be unavailable)")
        return None
    try:
        d = json.loads(f.read_text())
    except Exception:
        print("[runner] x_state.json unreadable — running logged-out")
        return None
    print("[runner] loaded X session")
    return {"cookies": d.get("cookies", []), "origins": d.get("origins", [])}


def build_tasks(rows) -> list:
    tasks = []
    for i, row in enumerate(rows, 1):
        capture_url = (row.get("link") or "").strip()
        if not capture_url:
            continue
        post_link = (row.get("post_link") or "").strip() or capture_url
        account = row.get("account_name") or f"tweet_{i}"
        safe = "".join(c if c.isalnum() else "_" for c in account)[:40]
        tasks.append({
            "idx": i, "capture_url": capture_url, "post_link": post_link,
            "account": account, "platform": "x",
            "category": row.get("category", "Uncategorized"),
            "shot": str(SHOTS / f"{i:02d}_{safe}.png"),
        })
    return tasks


def _shot_ok(result) -> bool:
    if result.get("status") != "ok":
        return False
    shot = result.get("screenshot")
    return bool(shot) and Path(shot).exists() and \
        Path(shot).stat().st_size > MIN_SHOT_BYTES


def _why_poor(result):
    """Why this shot is not trustworthy, or None when it is. Mirrors
    `src/run_report._why_poor`: the DOM fact first, the pixel analyzer after."""
    if result.get("overlay"):
        return "an X dialog was still covering the post"
    good, why = shot_quality.screenshot_quality(result["screenshot"])
    return None if good else why


def _quality_ok(result) -> bool:
    if not _shot_ok(result):
        return False
    return _why_poor(result) is None


def verify(collected) -> None:
    ok = [r for r in collected if _shot_ok(r)]
    bad = [r for r in collected if not _shot_ok(r)]
    print(f"[verify] {len(ok)}/{len(collected)} links produced a clean screenshot")
    for r in bad:
        why = r.get("status") if r.get("status") != "ok" else "empty/missing screenshot"
        print(f"[verify]   ✗ {r.get('account_name')}  ({why})  {r.get('post_link')}")


def _run(chunk, headless, state):
    return inf_worker.run_chunk(chunk, headless, state, CTX_KWARGS, SRC, INF)


def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    argv = sys.argv
    headless = "--headed" not in argv
    workers = int(_arg_value(argv, "--workers", DEFAULT_WORKERS))

    rows = input_loader.load(resolve_source(argv))
    tasks = build_tasks(rows)
    print(f"[runner] {len(tasks)} X link(s) loaded")
    if not tasks:
        print("[runner] nothing to capture")
        return

    state = x_storage_state()
    workers = max(1, min(workers, len(tasks)))

    chunks = [[] for _ in range(workers)]
    for n, t in enumerate(tasks):
        chunks[n % workers].append(t)
    chunks = [c for c in chunks if c]
    print(f"[runner] capturing with {len(chunks)} parallel worker(s)...")

    collected = []
    if len(chunks) == 1:
        collected = _run(chunks[0], headless, state)
    else:
        with ProcessPoolExecutor(max_workers=len(chunks)) as ex:
            futures = [ex.submit(inf_worker.run_chunk, c, headless, state,
                                 CTX_KWARGS, SRC, INF) for c in chunks]
            for fut in futures:
                collected.extend(fut.result())

    # retry pass — one sequential re-attempt for anything without a clean shot
    by_idx = {r["idx"]: r for r in collected}
    failed_idx = {r["idx"] for r in collected if not _shot_ok(r)}
    if failed_idx:
        retry_tasks = [t for t in tasks if t["idx"] in failed_idx]
        print(f"[runner] retrying {len(retry_tasks)} link(s) sequentially...")
        recovered = 0
        for r in _run(retry_tasks, headless, state):
            if _shot_ok(r) and not _shot_ok(by_idx.get(r["idx"], {})):
                recovered += 1
            if _shot_ok(r) or not _shot_ok(by_idx.get(r["idx"], {})):
                by_idx[r["idx"]] = r
        print(f"[runner] retry recovered {recovered}/{len(retry_tasks)}")

    # quality pass — recapture blank / black / half-loaded shots
    poor_idx = {i for i, r in by_idx.items() if _shot_ok(r) and not _quality_ok(r)}
    if poor_idx:
        poor_tasks = [t for t in tasks if t["idx"] in poor_idx]
        print(f"[quality] recapturing {len(poor_tasks)} low-quality screenshot(s)...")
        for t in poor_tasks:
            why = _why_poor(by_idx[t["idx"]]) or "unknown"
            print(f"[quality]   ↻ {by_idx[t['idx']].get('account_name')}  ({why})")
        fixed = 0
        for r in _run(poor_tasks, headless, state):
            by_idx[r["idx"]] = r
            if _quality_ok(r):
                fixed += 1
        print(f"[quality] improved {fixed}/{len(poor_tasks)}")

    # Final gate — same rule as the X runner: a dialog that survived every
    # retake is an observed fact, so the link is reported rather than printed
    # into the document. Pixel judgements never demote; they are heuristics.
    blocked = [r for r in by_idx.values()
               if r.get("status") == "ok" and r.get("overlay")]
    for r in blocked:
        r["status"] = "overlay_blocked"
    if blocked:
        print(f"[quality] dropping {len(blocked)} shot(s) still covered by an X dialog")

    collected = list(by_idx.values())
    collected.sort(key=lambda r: r.get("idx", 0))
    for r in collected:
        r.pop("idx", None)
        m = r.get("metrics") or {}
        print(f"  [x] {r['status']:12} {r.get('handle') or '':18} "
              f"F:{m.get('followers', '—'):>7} R:{m.get('reactions', '—'):>6} "
              f"C:{m.get('comments', '—'):>6} V:{m.get('reach', '—'):>7} "
              f"S:{m.get('shares', '—'):>6}  {r['account_name']}")

    verify(collected)

    missing = sum(1 for r in collected if _shot_ok(r)
                  and any(v == "—" for k, v in (r.get("metrics") or {}).items()
                          if not k.startswith("_")))
    if missing:
        print(f"[metrics] {missing} post(s) had at least one metric unavailable "
              "(shown as — in the report)")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(collected, indent=2))
    print(f"[runner] wrote {OUT / 'results.json'}")


if __name__ == "__main__":
    main()
