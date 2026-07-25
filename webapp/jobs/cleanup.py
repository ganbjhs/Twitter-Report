"""Disk retention: delete old jobs so a free-tier volume never fills up.

Two rules, both from `.env`:
  * age  — finished jobs older than RETENTION_DAYS are removed;
  * size — while data/jobs exceeds MAX_DATA_GB, the oldest finished job is
           removed, repeatedly.

Running jobs are never touched. A sweep runs at boot and then daily.
"""
import shutil
import threading
import time

from .. import config
from . import store

_SWEEP_INTERVAL = 24 * 60 * 60


def _dir_size(path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _remove_job(job_id: str) -> None:
    from . import runner
    shutil.rmtree(runner.job_dir(job_id), ignore_errors=True)
    store.delete(job_id)


def sweep() -> dict:
    """One retention pass. Returns a small summary for the log."""
    from . import runner

    removed_age = removed_size = 0
    cutoff = time.time() - config.RETENTION_DAYS * 86400
    jobs = store.list_all(limit=5000)

    finished = [j for j in jobs if j["status"] in store.DONE_STATES]
    for job in finished:
        when = job.get("finished_at") or job.get("created_at") or 0
        if when < cutoff:
            _remove_job(job["id"])
            removed_age += 1

    # Size cap — oldest finished job first.
    limit = config.MAX_DATA_GB * 1024 ** 3
    if config.JOBS_DIR.exists() and _dir_size(config.JOBS_DIR) > limit:
        survivors = [j for j in store.list_all(limit=5000)
                     if j["status"] in store.DONE_STATES]
        survivors.sort(key=lambda j: j.get("finished_at") or j.get("created_at") or 0)
        for job in survivors:
            if _dir_size(config.JOBS_DIR) <= limit:
                break
            _remove_job(job["id"])
            removed_size += 1

    # Orphaned folders with no database row (e.g. a crash mid-create).
    known = {j["id"] for j in store.list_all(limit=5000)}
    if config.JOBS_DIR.exists():
        for d in config.JOBS_DIR.iterdir():
            if d.is_dir() and d.name not in known:
                shutil.rmtree(d, ignore_errors=True)

    if removed_age or removed_size:
        print(f"[cleanup] removed {removed_age} expired and {removed_size} "
              f"oversize job(s)", flush=True)
    return {"expired": removed_age, "oversize": removed_size}


def start_scheduler() -> None:
    """Sweep at boot, then once a day, on a daemon thread."""
    def loop():
        while True:
            try:
                sweep()
            except Exception as e:                       # never kill the thread
                print(f"[cleanup] sweep failed: {e}", flush=True)
            time.sleep(_SWEEP_INTERVAL)

    threading.Thread(target=loop, name="cleanup", daemon=True).start()
