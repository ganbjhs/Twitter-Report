"""Run one report job: build an isolated working dir, invoke the pipeline as a
subprocess, follow its progress, publish the artifacts.

ISOLATION — why we copy the code instead of just setting cwd
------------------------------------------------------------
`src/run_report.py` and `src/report_builder.py` both anchor their output with

    ROOT = Path(__file__).resolve().parents[1]

That is derived from where the *source file physically lives*, not from the
current working directory — and `.resolve()` collapses symlinks, so symlinking
`src/` into a job folder would resolve straight back to the original and every
concurrent job would write into the same `reports/`.

So each job gets its own physical copy of the code tree (~90 KB of .py files;
single-digit milliseconds). `sessions/` stays a symlink to the one real cookie
file, because `x_storage_state()` only does `ROOT / "sessions" / "x_state.json"`
with no `.resolve()`.

Result: concurrent jobs cannot see or overwrite each other's output, and not one
line of the frozen pipeline had to change.
"""
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from .. import config, x_login
from . import store

# Code copied into each job's working directory.
_CODE_ITEMS = ("run.py", "src", "influencer")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "reports",
                                 "sessions")

# Live handles for cancellation, keyed by job id.
_RUNNING = {}
_RUNNING_LOCK = threading.Lock()


class JobFailed(Exception):
    pass


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def app_dir(job_id: str) -> Path:
    return job_dir(job_id) / "app"


def out_dir(job_id: str) -> Path:
    return job_dir(job_id) / "out"


def log_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.log"


# --------------------------------------------------------------------------- #
# Working directory
# --------------------------------------------------------------------------- #
def build_job_dir(job_id: str, rows: list, upload_bytes: bytes,
                  upload_name: str) -> Path:
    """Create data/jobs/<id>/ with a private copy of the code and the input."""
    from .. import uploads

    jd = job_dir(job_id)
    app = app_dir(jd.name)
    if jd.exists():
        shutil.rmtree(jd, ignore_errors=True)
    app.mkdir(parents=True, exist_ok=True)
    out_dir(job_id).mkdir(parents=True, exist_ok=True)

    for item in _CODE_ITEMS:
        src = config.ROOT / item
        if not src.exists():
            continue
        dst = app / item
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_IGNORE)
        else:
            shutil.copy2(src, dst)

    # One shared, read-only login cookie — symlinked, never copied around.
    link = app / "sessions"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(config.SESSIONS_DIR, target_is_directory=True)
    except OSError:
        # Filesystems without symlinks (rare): fall back to copying just the
        # state file so the job can still authenticate.
        link.mkdir(parents=True, exist_ok=True)
        if config.X_STATE_FILE.exists():
            shutil.copy2(config.X_STATE_FILE, link / "x_state.json")

    # Keep the original upload for troubleshooting (name is display-only).
    up = jd / "upload"
    up.mkdir(parents=True, exist_ok=True)
    (up / uploads.safe_upload_name(upload_name)).write_bytes(upload_bytes)

    uploads.write_canonical_xlsx(rows, app / "input.xlsx")
    return jd


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #
def build_command(report_type: str, title: str, date: str,
                  keep_engagement: bool = False, workers: int = 0) -> list:
    """The exact CLI invocation, identical in shape to what you run by hand.

    `--no-date` is what makes the document header read exactly what the user
    typed in "Report / File Name" and nothing else. `--date` is still passed so
    the invocation stays reproducible by hand; the switch simply keeps it out of
    the header.

    `--keep-engagement` is Twitter-only: the influencer capture already keeps
    likes and reposts in frame, so the switch would be meaningless there and is
    never passed to it.

    `workers` is the per-job capture speed, and like `--keep-engagement` it is
    Twitter-only. The influencer report keeps its INFLUENCER_WORKERS default
    whatever the form said: its follower-count cache lives in the worker
    PROCESS, so a second worker re-fetches the same profiles — more browsers
    there buy X more requests for the same data, not a faster report.

    0 falls back to the server default for the report type. The value is clamped
    to MAX_WORKERS here as well as at the API boundary, because this function is
    also reachable from a stored job record, and one browser too many is an OOM
    kill, not an error message.
    """
    influencer = report_type != "twitter"
    entry = str(Path("influencer") / "run_influencer.py") if influencer else "run.py"
    if influencer:
        workers = config.INFLUENCER_WORKERS
    else:
        workers = (min(workers, config.MAX_WORKERS) if workers > 0
                   else config.CAPTURE_WORKERS)
    cmd = [sys.executable, "-u", entry, "input.xlsx",
           "--title", title,
           "--date", date,
           "--no-date",
           "--workers", str(workers)]
    if keep_engagement and not influencer:
        cmd.append("--keep-engagement")
    return cmd


# --------------------------------------------------------------------------- #
# Progress parsing — reads the pipeline's own stdout, no pipeline changes
# --------------------------------------------------------------------------- #
_RE_TOTAL = re.compile(r"^\[runner\]\s+(\d+)\s+X link\(s\) loaded")
_RE_WORKERS = re.compile(r"^\[runner\]\s+capturing with (\d+) parallel worker")
_RE_RETRY = re.compile(r"^\[runner\]\s+retrying (\d+) link")
_RE_QUALITY = re.compile(r"^\[quality\]\s+recapturing (\d+)")
_RE_BLOCKED = re.compile(r"^\[quality\]\s+dropping (\d+) shot")
_RE_CROPPED = re.compile(r"^\[quality\]\s+(\d+) shot\(s\) may be missing")
_RE_VERIFY = re.compile(r"^\[verify\]\s+(\d+)/(\d+) links produced")
_RE_RESULT = re.compile(r"^\s+\[x\]\s+(\S+)\s+(.*)$")
_RE_SKIPPED = re.compile(r"^\[input\]\s+skipped (\d+) non-X link")
_RE_NO_SESSION = re.compile(r"^\[runner\]\s+NO saved X session")
_RE_METRICS = re.compile(r"^\[metrics\]\s+(\d+) post\(s\) had at least one metric")
_RE_WROTE = re.compile(r"^\[report\]\s+wrote\s+(.+?)\s+\(")


class _Progress:
    """Translates pipeline stdout into job state + an activity log for the UI."""

    def __init__(self, job_id: str, total: int):
        self.job_id = job_id
        self.total = total
        self.done = 0
        self.phase = "Starting the browser"
        self.login_wall = False
        self._last_push = 0.0

    def note(self, message: str, level: str = "info") -> None:
        store.append_activity(self.job_id, message, level)

    def _push(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_push > 1.0:
            self._last_push = now
            store.update(self.job_id, phase=self.phase, done=self.done,
                         total=self.total)

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self._push(force=True)

    def observe_shots(self, count: int) -> None:
        """Live count from the screenshots folder while capture is running."""
        if count > self.done:
            self.done = min(count, self.total or count)
            self._push()

    def line(self, text: str) -> None:
        m = _RE_TOTAL.match(text)
        if m:
            self.total = int(m.group(1))
            self.set_phase("Capturing posts")
            self.note(f"{self.total} X link(s) loaded from the uploaded file.")
            return
        m = _RE_SKIPPED.match(text)
        if m:
            self.note(f"{m.group(1)} non-X link(s) in the file were skipped.",
                      "warn")
            return
        if _RE_NO_SESSION.match(text):
            self.login_wall = True
            self.note("No saved X login found on the server — posts will hit a "
                      "login wall. An admin needs to upload sessions/x_state.json.",
                      "error")
            return
        m = _RE_WORKERS.match(text)
        if m:
            self.set_phase(f"Capturing posts ({m.group(1)} parallel browser(s))")
            return
        m = _RE_RETRY.match(text)
        if m:
            self.set_phase("Retrying links that failed the first pass")
            self.note(f"Retrying {m.group(1)} link(s) that did not capture cleanly.",
                      "warn")
            return
        m = _RE_QUALITY.match(text)
        if m:
            self.set_phase("Re-capturing low-quality screenshots")
            self.note(f"Re-capturing {m.group(1)} screenshot(s) that came out "
                      "blank, black, half-loaded or covered by an X dialog.",
                      "warn")
            return
        m = _RE_BLOCKED.match(text)
        if m:
            self.note(f"{m.group(1)} post(s) still had an X dialog on top after "
                      "every retake — left out rather than shown as a popup.",
                      "warn")
            return
        m = _RE_CROPPED.match(text)
        if m:
            self.note(f"{m.group(1)} screenshot(s) may not show the whole post "
                      "and its reply — check them in screenshots.zip.", "warn")
            return
        m = _RE_VERIFY.match(text)
        if m:
            good, total = int(m.group(1)), int(m.group(2))
            self.done, self.total = good, total
            self.set_phase("Building the document")
            level = "info" if good == total else "warn"
            self.note(f"Verified: {good}/{total} links produced a clean screenshot.",
                      level)
            return
        m = _RE_RESULT.match(text)
        if m and m.group(1) != "ok":
            status_txt, who = m.group(1), m.group(2).strip()
            self.note(f"Could not capture {who or 'a post'} — {status_txt}.", "warn")
            return
        m = _RE_METRICS.match(text)
        if m:
            self.note(f"{m.group(1)} post(s) had at least one engagement metric "
                      "unavailable — those show as — in the report.", "warn")
            return
        m = _RE_WROTE.match(text)
        if m:
            self.set_phase("Packaging downloads")
            return


def _shot_count(app: Path) -> int:
    shots = app / "reports" / "screenshots"
    try:
        return sum(1 for p in shots.iterdir() if p.suffix.lower() == ".png")
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #
def _zip_screenshots(app: Path, dest: Path) -> bool:
    shots = app / "reports" / "screenshots"
    files = sorted(p for p in shots.glob("*.png")) if shots.exists() else []
    if not files:
        return False
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, arcname=f"screenshots/{p.name}")
    return True


def _read_results(app: Path) -> list:
    f = app / "reports" / "results.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return []


_STATUS_REASON = {
    "login_wall": "X asked for a login — the server's X session may have expired",
    "not_found": "post unavailable, deleted, protected or suspended",
    "age_restricted": "X age-restricted this post and only accepts age "
                      "verification through its mobile app, so the content "
                      "cannot be shown in a desktop capture",
    "overlay_blocked": "an X dialog stayed on top of the post through every "
                       "retake, so the screenshot showed the popup instead",
}


def _skipped_from_results(results: list) -> list:
    """Links that did NOT make it into the document, with a plain-English why.

    The document itself drops them (matching the Twitter report's behaviour);
    this list is what the UI's activity panel shows.
    """
    skipped = []
    for r in results:
        status_txt = r.get("status") or "error"
        shot = r.get("screenshot")
        has_shot = bool(shot) and Path(shot).exists() and Path(shot).stat().st_size > 0
        if status_txt == "ok" and has_shot:
            continue
        if status_txt.startswith("error"):
            reason = status_txt[6:].strip(": ") or "capture error"
        else:
            reason = _STATUS_REASON.get(status_txt,
                                        "screenshot did not render" if status_txt == "ok"
                                        else status_txt)
        skipped.append({
            "account": r.get("account_name") or r.get("handle") or "",
            "link": r.get("post_link") or r.get("url") or "",
            "reason": reason,
        })
    return skipped


def publish(job_id: str, app: Path, stem: str) -> dict:
    """Move the pipeline's output into out/ under the user's chosen name."""
    out = out_dir(job_id)
    out.mkdir(parents=True, exist_ok=True)
    reports = app / "reports"
    artifacts = {}

    for ext in ("pdf", "docx"):
        produced = sorted(reports.glob(f"*.{ext}"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        if produced:
            dest = out / f"{stem}.{ext}"
            shutil.copy2(produced[0], dest)
            artifacts[ext] = dest.name

    zip_dest = out / f"{stem}_screenshots.zip"
    if _zip_screenshots(app, zip_dest):
        artifacts["zip"] = zip_dest.name

    return artifacts


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #
def _register(job_id: str, proc) -> None:
    with _RUNNING_LOCK:
        _RUNNING[job_id] = proc


def _unregister(job_id: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.pop(job_id, None)


def cancel(job_id: str) -> bool:
    """Kill a running job's process group (the capture forks worker processes)."""
    with _RUNNING_LOCK:
        proc = _RUNNING.get(job_id)
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            proc.terminate()
        except OSError:
            return False
    return True


# --------------------------------------------------------------------------- #
# The job itself
# --------------------------------------------------------------------------- #
def run_job(job_id: str, on_line=None) -> dict:
    """Execute one job start to finish. Web-framework agnostic, so the same
    function backs both the background queue and the inline (scale-to-zero)
    execution mode.

    `on_line(text)` — optional callback fed every stdout line, used by the
    inline mode to stream progress back over the open HTTP connection.
    """
    job = store.get(job_id)
    if not job:
        raise JobFailed(f"Unknown job {job_id}")
    if job["status"] in store.DONE_STATES:
        return job          # cancelled (or already finished) before its turn came

    app = app_dir(job_id)
    stem = job["name"]
    date = datetime.date.today().strftime("%d-%m-%y")
    keep_engagement = bool(job.get("keep_engagement"))
    cmd = build_command(job["report_type"], job["title"], date, keep_engagement,
                        int(job.get("workers") or 0))

    store.update(job_id, status="running", started_at=time.time(),
                 phase="Checking the X login", error="")
    prog = _Progress(job_id, job.get("total") or job.get("link_count") or 0)
    prog.note(f"Job started — {job['report_type']} report, "
              f"{job.get('link_count', 0)} link(s).")
    chosen = int(job.get("workers") or 0)
    if chosen and job["report_type"] == "twitter":
        capped = min(chosen, config.MAX_WORKERS)
        # "up to": the pipeline also caps workers at the number of links, so a
        # 4-browser choice on 2 links really runs 2.
        prog.note(f"Capturing with up to {capped} browser(s)" +
                  (f" — {chosen} was above this server's limit of "
                   f"{config.MAX_WORKERS}." if capped < chosen else "."))
    if keep_engagement and job["report_type"] == "twitter":
        prog.note("Screenshots will keep the engagement line (replies, reposts, "
                  "likes, views) — on a comment link, the parent's line and the "
                  "comment's own.")

    # The cookie file lives on an ephemeral disk on free hosts, so make sure a
    # valid X session exists before the capture starts. Cheap when it already
    # does; signs in headlessly when it does not.
    ok, message = x_login.ensure_session()
    if not ok:
        prog.note(f"X login: {message}", "warn")
    elif "Signed in" in message:
        prog.note("Signed in to X automatically (the saved session was missing "
                  "or expired).")
    prog.set_phase("Starting the browser")

    log = log_path(job_id).open("w", encoding="utf-8")
    log.write("$ " + " ".join(cmd) + "\n\n")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        cmd, cwd=str(app), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        start_new_session=True)          # own process group -> cancellable
    _register(job_id, proc)

    # Watchdog: hard timeout so a wedged browser can't pin a capture slot forever.
    timed_out = {"hit": False}

    def _watchdog():
        deadline = time.time() + config.JOB_TIMEOUT_MINUTES * 60
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(2)
        if proc.poll() is None:
            timed_out["hit"] = True
            cancel(job_id)

    # Live progress: the per-link result lines only print after capture ends, so
    # we count screenshot files on disk to drive the "captured 12 / 40" bar.
    def _shot_poller():
        while proc.poll() is None:
            prog.observe_shots(_shot_count(app))
            time.sleep(2)

    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_shot_poller, daemon=True).start()

    try:
        for raw in proc.stdout:
            text = raw.rstrip("\n")
            log.write(raw)
            prog.line(text)
            if on_line:
                try:
                    on_line(text)
                except Exception:
                    pass
        code = proc.wait()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        log.close()
        _unregister(job_id)

    results = _read_results(app)
    skipped = _skipped_from_results(results)
    artifacts = publish(job_id, app, stem)
    captured = len(results) - len(skipped)

    # Success means a DOCUMENT was produced. A failed link still leaves an
    # evidence screenshot behind, so screenshots.zip alone is not success —
    # without this the "every link was unavailable" case would report "done".
    has_document = bool(artifacts.get("pdf") or artifacts.get("docx"))

    for s in skipped:
        prog.note(f"Not in the report: {s['account'] or s['link']} — {s['reason']}",
                  "warn")

    if any(r.get("status") == "login_wall" for r in results):
        prog.login_wall = True
        # The cookie exists but X is not honouring it. Drop it so the next job
        # signs in again — but only if the server can sign in; otherwise the
        # hand-uploaded file is all there is and must be kept.
        if x_login.invalidate("captures hit an X login wall"):
            prog.note("Discarded the rejected X session — the next report will "
                      "sign in again.", "warn")

    finished = time.time()
    # A user cancellation already set the final status. The subprocess then exits
    # with SIGTERM (-15), which must NOT be re-reported as a failure.
    if (store.get(job_id) or {}).get("status") == "cancelled":
        store.update(job_id, finished_at=finished, phase="Cancelled",
                     skipped=skipped, artifacts=artifacts, done=captured)
        return store.get(job_id)

    if timed_out["hit"]:
        store.update(job_id, status="failed", finished_at=finished,
                     phase="Timed out", skipped=skipped, artifacts=artifacts,
                     error=f"The job ran longer than {config.JOB_TIMEOUT_MINUTES} "
                           "minutes and was stopped.")
        prog.note("Job stopped: exceeded the time limit.", "error")
    elif code != 0 and not has_document:
        tail = _log_tail(job_id)
        store.update(job_id, status="failed", finished_at=finished,
                     phase="Failed", skipped=skipped, artifacts=artifacts,
                     error=f"The capture process exited with code {code}.\n{tail}")
        prog.note(f"Job failed — process exited with code {code}.", "error")
    elif not has_document:
        reason = (("Every link hit an X login wall — the X session was rejected. "
                   "The server will try to sign in again on the next run; if "
                   "this keeps happening, check the X account credentials on "
                   "the X login page."
                   if x_login.credentials_configured() else
                   "Every link hit an X login wall — the server's saved X "
                   "session has expired and no X account is configured to sign "
                   "in with. See the X login page.")
                  if prog.login_wall else
                  "No link produced a usable screenshot, so there was nothing "
                  "to put in the report. See the list below for why each link "
                  "failed.")
        store.update(job_id, status="failed", finished_at=finished,
                     phase="Failed", done=0, skipped=skipped,
                     artifacts=artifacts, error=reason)
        prog.note(reason, "error")
    else:
        store.update(job_id, status="done", finished_at=finished,
                     phase="Done", done=captured, total=len(results) or prog.total,
                     artifacts=artifacts, skipped=skipped)
        note = f"Report ready — {captured} post(s) included."
        if skipped:
            note += f" {len(skipped)} link(s) could not be captured and were left out."
        prog.note(note, "info")
        if prog.login_wall:
            prog.note("Some posts hit an X login wall — the server's X session "
                      "may be expiring. Consider refreshing it.", "warn")

    return store.get(job_id)


def _log_tail(job_id: str, lines: int = 12) -> str:
    try:
        text = log_path(job_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
