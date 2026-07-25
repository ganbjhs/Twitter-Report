"""Background job queue.

A bounded ThreadPoolExecutor whose threads each supervise one capture
subprocess. That is deliberately the whole design:

  * the real work already happens in child processes (the pipeline forks its own
    Playwright workers), so there is no GIL contention to engineer around;
  * MAX_CONCURRENT_JOBS is what keeps Chromium's RAM inside a free-tier VM, and a
    fixed-size pool expresses that directly;
  * a broker (Celery/RQ + Redis) would add a second service and ~100 MB of RAM to
    solve a queueing problem this already solves at office scale.

Durability comes from SQLite, not from the queue: on boot, `store.init()` marks
anything left `running` as `interrupted`, so a restart can never leave the UI
claiming a dead job is still going.
"""
import atexit
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from .. import config
from . import runner, store

_pool = None
_lock = threading.Lock()


def start() -> None:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=config.MAX_CONCURRENT_JOBS,
                thread_name_prefix="capture")
            atexit.register(shutdown)


def shutdown() -> None:
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def submit(job_id: str) -> None:
    """Queue a job. Returns immediately — the HTTP request never blocks."""
    start()
    _pool.submit(_run_guarded, job_id)


def _run_guarded(job_id: str) -> None:
    try:
        runner.run_job(job_id)
    except Exception as e:                       # never let a thread die silently
        detail = traceback.format_exc(limit=4)
        store.update(job_id, status="failed", phase="Failed",
                     error=f"Internal error while running the job: {e}")
        store.append_activity(job_id, f"Internal error: {e}", "error")
        print(f"[queue] job {job_id} crashed:\n{detail}", flush=True)


def cancel(job_id: str) -> bool:
    """Stop a running job. Queued-but-not-started jobs are marked cancelled so
    the runner refuses to start them."""
    job = store.get(job_id)
    if not job or job["status"] in store.DONE_STATES:
        return False
    killed = runner.cancel(job_id)
    store.update(job_id, status="cancelled", phase="Cancelled",
                 error="Cancelled by the user.")
    store.append_activity(job_id, "Job cancelled by the user.", "warn")
    return killed or job["status"] == "queued"
