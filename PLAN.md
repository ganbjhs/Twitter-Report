# Implementation Plan — X/Twitter Report web app + Influencer report

Status: **awaiting approval**. No code written yet.

Headline finding: **zero changes to the existing X pipeline are required.** Not one line of
`run.py`, `src/run_report.py`, `src/_worker.py`, `src/capture/*`, `src/report_builder.py`,
`src/input_loader.py`, `src/platforms.py`, `src/shot_quality.py`. See §7.

---

## 1. Tech stack

| Layer | Choice |
|---|---|
| Web framework | **FastAPI** + **Uvicorn** |
| Templates | **Jinja2** server-rendered HTML + a little vanilla JS (fetch polling) |
| Session | Signed httpOnly cookie via `itsdangerous` (Starlette `SessionMiddleware`) |
| Passwords | `passlib[bcrypt]`-hashed, or plain in `.env` compared with `hmac.compare_digest` |
| Job store | **SQLite** (`jobs.db`, stdlib `sqlite3`) |
| Job execution | In-process **ThreadPoolExecutor** + global semaphore, each job = a `subprocess` |
| Input normalizing | `openpyxl` (already a dep) + `xlrd` (for legacy `.xls`) |
| Container | Docker, `mcr.microsoft.com/playwright/python` base (has arm64 + Chromium OS deps) |

**Justification (one paragraph).** A Python backend is mandatory here — the entire value of this
project is the existing Playwright pipeline, and FastAPI lets me invoke it in-process or as a
subprocess with no IPC bridge or language boundary. I chose server-rendered Jinja2 templates over
an SPA deliberately: the whole UI is a login form, an upload form, a status line and three download
buttons. An SPA would add Node, a build step, a bundle to serve, and a second thing to deploy, in
exchange for nothing this UI needs — polling a JSON status endpoint from 30 lines of vanilla JS
gives the identical experience. SQLite over Postgres/Redis for the same reason: one file, no
service, survives restarts, and the write volume is a handful of rows per day. That keeps the whole
deployment to a single container (plus an optional Caddy sidecar for HTTPS), which is what makes
the free-tier hosting in §8 actually maintainable.

---

## 2. Architecture — wrapping the frozen CLI

### 2.1 Invocation: subprocess, not import

Each job runs the existing entrypoint exactly as you run it today:

```
python run.py <job-dir>/input.xlsx --title "<sanitized name>" --date <dd-mm-yy> --workers <N>
```

Subprocess over import, because:
- `run.py` and `run_report.py` communicate through `sys.argv` and mutate it ([run.py:47](run.py#L47),
  [run.py:51](run.py#L51)). Importing them into a long-lived web server means global-state
  clobbering across concurrent jobs.
- `run_report.main()` spawns a `ProcessPoolExecutor`; nesting that under a web server's worker
  threads is fragile. A clean child process sidesteps it entirely.
- A crashed/hung capture kills a child, not the web server. It's also killable (job cancel) and
  its exit code is a real signal.
- stdout gives me a free progress feed (§3.2).

### 2.2 Per-job isolation: copy the code tree (NOT cwd)

**The problem.** `ROOT = Path(__file__).resolve().parents[1]` in
[run_report.py:30](src/run_report.py#L30) and [report_builder.py:23](src/report_builder.py#L23) means
`reports/`, `sessions/` and `config/` are anchored to *where the source file physically lives*, not
to cwd. `.resolve()` also collapses symlinks, so symlinking `src/` into a job dir resolves straight
back to the original and all jobs would collide in one `reports/` folder.

**The fix.** Give each job its own *physical copy of the code*. The code is ~90 KB of `.py` files —
copying it costs single-digit milliseconds and is entirely invisible next to a multi-minute capture.

```
data/jobs/<job_id>/
├── app/                      # per-job copy of the frozen code (fresh each job)
│   ├── run.py
│   ├── src/…                 # copied verbatim, never edited
│   ├── sessions/             # SYMLINK → /data/secrets/sessions  (read-only, shared)
│   ├── input.xlsx            # the normalized upload
│   └── reports/              # ← job-private output: PDF, DOCX, results.json, screenshots/
├── upload/<original filename>
├── job.log                   # captured stdout/stderr
└── out/                      # published artifacts: <name>.pdf, <name>.docx, screenshots.zip
```

`sessions/` stays a symlink: `x_storage_state()` ([run_report.py:65](src/run_report.py#L65)) only
does `ROOT / "sessions" / "x_state.json"` and `.read_text()` — no `.resolve()` — so the symlink is
followed correctly and the cookie exists in exactly one place on disk.

This is fully general: it works identically for the Influencer path, and it means two colleagues
submitting at the same second can never see each other's `reports/`.

### 2.3 Input normalization (upload → canonical `.xlsx`)

The web layer converts **every** accepted upload into a canonical single-sheet `.xlsx` with a
`Category | Account | Link` header row before handing it to the pipeline. This:

1. Fixes two real gaps in `input_loader` **without editing it**:
   - `.xls` (legacy binary): `load_excel` → `BadZipFile` → falls back to reading binary as text → garbage.
   - `.tsv`: `load_delimited` uses `csv.reader` with the **comma** dialect, so `Name<TAB>URL`
     comes back as one cell and the link ends up as `"Name\thttps://…"`.
2. Satisfies §6 ("validate that uploaded files parse before enqueuing") — I parse, count, and
   validate links up front and reject a bad file with a useful message *before* a job is queued.
3. Lets me show "42 links found" on the confirmation screen.

Accepted: `.xlsx .xls .csv .tsv .txt`. Normalizer keeps the exact input order, preserves category
and account columns when present, and drops non-X links with a warning surfaced in the UI.

---

## 3. Background jobs & concurrency

### 3.1 Design

- A module-level `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS)` started with the app.
- Each pool thread does: build job dir → `subprocess.Popen(...)` → stream stdout → publish artifacts.
- Job state lives in SQLite (`id, owner, name, type, status, total, done, created, finished, error`),
  so state survives a restart; on boot, any `running` row is marked `interrupted`.
- **RAM guard (§5 resource note):** `MAX_CONCURRENT_JOBS=1` and `CAPTURE_WORKERS=2` by default —
  i.e. at most 2 Chromium processes ≈ 0.6–1.4 GB, comfortable on a 12 GB Oracle VM and survivable on
  smaller hosts. Both are env vars, so you can raise them without a rebuild.
- Extra submissions sit in the executor's queue → status `queued`.

**Why not Celery/RQ/Redis:** they'd add a broker service, a second container, and ~100 MB RAM to
solve a queueing problem that a bounded thread pool already solves at this scale (a few jobs/day,
one office). The heavy lifting is already in child processes, so there's no GIL concern. Fewer
moving parts is a deployment feature, not a compromise, on a free tier.

### 3.2 Progress signal

The runner already prints exactly what I need — I parse stdout, no code changes:

| Line printed by the pipeline | Meaning |
|---|---|
| `[runner] 40 X link(s) loaded` ([run_report.py:132](src/run_report.py#L132)) | total = 40 |
| `  [x] ok           @handle  Name` ([run_report.py:192](src/run_report.py#L192)) | +1 captured |
| `[runner] retrying N link(s)…` / `[quality] recapturing N…` | phase = retry / quality |
| `[verify] 38/40 links produced a clean screenshot` | verified count |
| `[report] wrote …pdf` ([report_builder.py:301](src/report_builder.py#L301)) | building → done |
| `[runner] NO saved X session` ([run_report.py:69](src/run_report.py#L69)) | → "admin must re-upload login" |

Caveat I'll handle: the per-result `[x]` lines are printed in a batch after capture finishes, not
live. So during capture the UI shows phase + elapsed, and I additionally poll
`reports/screenshots/*.png` file count for a true live "captured 12 / 40". A `login_wall`-heavy
`results.json` triggers the expired-cookie banner.

### 3.3 Dual-mode execution (§5 "either host without a rewrite")

The job runner is a single function `run_job(job_id) -> JobResult` with no web dependency.
- **Always-on VM:** the thread pool calls it; the browser polls `/api/jobs/{id}`.
- **Scale-to-zero (Cloud Run):** `POST /jobs?inline=1` calls the same function inside the request
  and streams progress lines back (`StreamingResponse`), holding the connection so the container
  isn't frozen. Toggled by `EXECUTION_MODE=queue|inline` in env. No code fork.

---

## 4. File layout — new code, clearly separated

```
Twitter-Report/
├── run.py, src/…                 ← FROZEN. Untouched.
│
├── webapp/                       ← NEW: everything web
│   ├── main.py                   FastAPI app, middleware, startup
│   ├── config.py                 env/settings loading
│   ├── auth.py                   login/logout, session, rate limit, CSRF
│   ├── routes_jobs.py            submit / status / download
│   ├── jobs/
│   │   ├── store.py              SQLite job table
│   │   ├── queue.py              thread pool + semaphore
│   │   ├── runner.py             job dir build, subprocess, stdout parse, publish
│   │   └── cleanup.py            retention sweep
│   ├── uploads.py                normalizer + validation (§2.3)
│   ├── templates/                login.html, index.html, job.html, base.html
│   └── static/                   app.css, app.js
│
├── influencer/                   ← NEW: the parallel Influencer pipeline
│   ├── run_influencer.py         entrypoint (mirrors run.py's CLI surface)
│   ├── inf_runner.py             mirrors run_report.py (tasks, workers, retries, verify)
│   ├── inf_worker.py             parallel worker — imports inf_capture DIRECTLY
│   ├── inf_capture.py            capture keeping engagement + reading metrics
│   └── inf_report_builder.py     the styled PDF + DOCX
│
├── data/                         ← gitignored runtime state
│   ├── jobs/<job_id>/…
│   └── jobs.db
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── DEPLOY.md
```

`influencer/` imports the frozen `src/shot_quality.py`, `src/input_loader.py` and `src/platforms.py`
read-only (allowed explicitly by your prompt); it never imports `_worker.py` or `capture/`.

---

## 5. Routes & flow

| Method | Route | Purpose |
|---|---|---|
| GET/POST | `/login` | credential form; rate-limited; sets signed session |
| POST | `/logout` | clears session |
| GET | `/` | upload form (auth required) |
| POST | `/api/jobs` | multipart: file + name + report_type → validate → create job → `202 {job_id}` |
| GET | `/jobs/{id}` | status page |
| GET | `/api/jobs/{id}` | JSON: `{status, phase, done, total, error, artifacts[]}` |
| GET | `/api/jobs/{id}/download/{pdf\|docx\|zip}` | streams artifact, **scoped to the owning session** |
| POST | `/api/jobs/{id}/cancel` | kills the child process |
| GET | `/healthz` | unauthenticated liveness (needed by Cloud Run) |
| GET | `/admin/session-status` | shows X-cookie validity/age, re-upload instructions |

Flow: upload → normalize+validate (sync, fast) → `202` + redirect to `/jobs/{id}` → JS polls every
2 s → on `done`, three download buttons appear.

**Security (§6):** signed httpOnly+SameSite=Lax+Secure cookie; per-form CSRF token; login rate limit
(5 attempts / 15 min per IP, backed by SQLite); `secrets.compare_digest` on credentials; report name
sanitized to `[A-Za-z0-9._-]` and length-capped (same spirit as [run.py:44](run.py#L44)); uploaded
filename never used as a path, only for display; `MAX_UPLOAD_MB=5` and `MAX_LINKS=200` enforced;
downloads resolve through the job record, never a user-supplied path; `sessions/` and `.env` are
never served — no static route can reach them. Retention: nightly sweep deleting jobs older than
`RETENTION_DAYS=7`, plus a `MAX_DATA_GB=5` oldest-first eviction.

---

## 6. Influencer report spec

### 6.1 Capture (`influencer/inf_capture.py`)

Structurally mirrors `x_capture.py` — same load/retry/sensitive-gate/quality-retake discipline,
which is battle-tested — with three deliberate differences:

1. **Crop keeps engagement.** `x_capture._crop_box` cuts at the *top* of `[role="group"]`. Mine cuts
   at the **bottom of `[role="group"]` + padding**, so display name, @handle, text, media and the
   likes/reposts row are all inside the frame.
2. **Right-article selection for replies.** On a reply URL, `article[data-testid="tweet"]`**.first**
   is the *parent* tweet, not the one you linked. I select the article whose timestamp anchor href
   contains the status ID parsed from the URL, falling back to `.first`. This is what makes replies
   work correctly.
3. **Metrics read off the page.** Primary source is the `aria-label` on the tweet's `[role="group"]`,
   which X renders as `"12 replies, 3 reposts, 45 likes, 6 bookmarks, 89012 views"` — one parse,
   all four numbers, locale-tolerant. Fallbacks: individual `[data-testid="like"|"reply"|"retweet"]`
   aria-labels, and the `/analytics` anchor for views. Anything unreadable → `"—"`.

Video/reels: wait on `[data-testid="videoPlayer"]` and its poster image alongside the existing
`_ALL_MEDIA_READY` check, so we shoot a rendered poster frame rather than a black box. The existing
`shot_quality` analyzer stays in the loop and forces a retake on a black frame.

Result dict adds: `{"metrics": {"reactions", "comments", "reach", "shares"}}` on top of the existing
contract, so `results.json` stays a superset — nothing downstream breaks.

Metric mapping (as specified): Reactions←Likes, Comments←Replies, Reach←Views, Shares←Reposts.
Numbers rendered in X's own compact form (`1.2K`, `45.6M`) for a clean table.

### 6.2 Layout (`influencer/inf_report_builder.py`) — identical in PDF and DOCX

```
┌──────────────────────────────────────────────┐
│            Influencer Report 25-07-26        │  ← title bar, centered
├──────────────────────────────────────────────┤  ← separator rule
│                                              │
│            ┌──────────────────┐              │
│            │   screenshot     │              │  ← username+text+media+likes/reposts
│            │   (centered)     │              │
│            └──────────────────┘              │
│                                              │
│  ┌───────────┬──────────┬────────┬────────┐  │
│  │ Reactions │ Comments │ Reach  │ Shares │  │  ← tinted header row
│  ├───────────┼──────────┼────────┼────────┤  │
│  │   12.4K   │   831    │ 1.2M   │  3.4K  │  │
│  └───────────┴──────────┴────────┴────────┘  │
│                                              │
│  Link — https://x.com/…                      │  ← clickable
└──────────────────────────────────────────────┘
                   … one post per page …

Links                                             ← final table, single column,
┌──────────────────────────────────────────────┐    NO serial numbers, input order,
│ Link                                         │    every URL clickable
├──────────────────────────────────────────────┤
│ https://x.com/…                              │
```

Styling direction (my creative call, tell me if you want different): X-blue `#1D9BF0` accent,
neutral slate `#0F172A` text on white, Helvetica, generous whitespace, hairline `#E2E8F0` table
rules, metric values large and semibold above small uppercase-tracked labels. One post per page.

### 6.3 Decisions I need you to confirm

| # | Question | My default if you don't care |
|---|---|---|
| D1 | **Shares** = reposts only, or reposts + quotes? | Reposts only (matches the on-screen number) |
| D2 | **Reach** — views are logged-in-visible but absent on some older/protected posts. Confirm `—` is fine there? | Yes, render `—` |
| D3 | Include posts that **failed** to capture? The Twitter report silently drops them ([report_builder.py:40](src/report_builder.py#L40)). Same for Influencer? | Drop from the document, list them in the UI as skipped |
| D4 | Keep the small centered **category** label above each shot (the Twitter report shows it when categories exist)? Your §3.2 layout doesn't mention it. | Keep it, same behavior |
| D5 | Page size: **Letter** (what the existing report uses) or **A4** (India-standard)? | Letter, to match the existing report |
| D6 | Header text: your "File Name" field **plus today's date** (`"<Name> 25-07-26"`, exactly like the CLI), or the raw name only? | Name + date |
| D7 | `screenshots.zip` — screenshots only, or also include `results.json`? | Screenshots only |
| D8 | Should a logged-in colleague see **only their own** jobs, or all jobs (shared office tool)? | Own jobs only; downloads scoped to session |
| D9 | Retention: delete jobs after **7 days**? | 7 days + 5 GB cap |
| D10 | Metric numbers: X's compact form (`12.4K`) or full (`12,431`)? | Compact, as shown on X |

---

## 7. Existing X code I would need to touch

**None.** Explicitly:

| Risk your prompt flagged | Verdict |
|---|---|
| §2.3 per-job isolation | Solved by the per-job code copy (§2.2). No edit. |
| §3.3 `src/capture/__init__.py` dispatcher | **Not needed.** `influencer/inf_worker.py` imports `inf_capture` directly; the frozen dispatcher is never involved. Option (a) from your prompt. |
| §3.3 `src/_worker.py` | **Not needed.** `inf_worker.py` is a parallel file, a near-copy of the 35-line worker pointed at the new capture. `_worker.py` is untouched. |
| `.tsv` / `.xls` weaknesses in `input_loader.py` | Solved *outside* it by the upload normalizer (§2.3). No edit. |
| `.gitignore` | Needs `data/`, `.env`, `PLAN.md`-adjacent additions. This is config, not X-report code — I'll do it unless you object. |
| `requirements.txt` | I'll add `requirements-web.txt` as a **separate file** rather than editing yours. |

If anything unforeseen appears mid-build that seems to need a frozen-file edit, I stop and ask,
per your rule #2.

---

## 8. Deployment — zero cost, always on

**Recommendation: Oracle Cloud "Always Free" ARM VM (`VM.Standard.A1.Flex`, 2 OCPU / 12 GB), Docker
Compose.** It's the only genuinely free-forever option that behaves like a real server: no cold
starts, no request-timeout ceiling, real persistent disk for job outputs and the login cookie, and
the background-worker model (which is the better UX for multi-minute captures) works unmodified.
12 GB of RAM is generous headroom for Chromium. Documented caveats: card required for identity
verification only; ARM capacity can be "out of capacity" at creation (retry / other AD / other
region); you own OS patching.

**Fallback: Google Cloud Run** (scale-to-zero, free tier). Same image, `EXECUTION_MODE=inline`
(§3.3) so capture runs inside the request with a streamed progress feed. Trade-off to accept:
~30 s cold start, a 60 min request ceiling, and outputs must go to a bucket or be downloaded in the
same session since the filesystem is ephemeral. Not targeting Vercel or any function runtime, per
your instruction.

**Deliverables:**
- `Dockerfile` — `mcr.microsoft.com/playwright/python:v1.4x-jammy` (arm64 + amd64, Chromium and all
  OS deps preinstalled — avoids the fragile `install-deps` dance on ARM), non-root user, app + deps.
- `docker-compose.yml` — one `web` service (serves + runs the pool), named volumes `data` (jobs) and
  `secrets` (the X cookie), optional `caddy` service for free auto-HTTPS.
- `.env.example` — `APP_USERS`, `SESSION_SECRET`, `MAX_CONCURRENT_JOBS`, `CAPTURE_WORKERS`,
  `MAX_UPLOAD_MB`, `MAX_LINKS`, `RETENTION_DAYS`, `EXECUTION_MODE`.
- **X cookie path:** you run `python save_login.py x` on your Mac as today, then
  `scp sessions/x_state.json ubuntu@<IP>:~/app/secrets/x_state.json` and `docker compose restart`.
  Never committed, never in the image, never reachable by any route. The app detects a missing /
  expired cookie (`login_wall` statuses, or the `[runner] NO saved X session` line) and shows a
  clear "admin must re-upload the login" banner with these exact steps.
- `DEPLOY.md` — the beginner-friendly walkthrough (your Appendix A, corrected and verified: VM
  creation, VCN ingress, the `iptables` gotcha, Docker install, secrets, `compose up -d --build`,
  Caddy HTTPS, cookie refresh) plus the Cloud Run fallback section.

---

## 9. Build order & checkpoints

| Phase | Deliverable | Your checkpoint |
|---|---|---|
| **0** | `.env.example`, `requirements-web.txt`, `.gitignore` additions, skeleton | — |
| **1** | Auth: login page, session cookie, CSRF, rate limit, logout | Log in/out locally |
| **2** | Upload UI + normalizer + validation (all 5 formats) | Upload each format, see "N links found" |
| **3** | Job engine: dirs, subprocess, progress, SQLite, downloads — **Twitter report end to end** | **Generate a real Twitter report through the browser, byte-identical to the CLI's** |
| **4** | `inf_capture.py` + `inf_worker.py` + `inf_runner.py` — capture only | **Review sample screenshots: engagement in frame, replies + video correct, metrics parsed** |
| **5** | `inf_report_builder.py` — the styled PDF + DOCX | **Review a real Influencer PDF/DOCX** |
| **6** | Wire Influencer into the web app; concurrency + isolation test (2 simultaneous jobs) | Both jobs correct, no cross-contamination |
| **7** | Cleanup/retention, admin session-status page, error surfaces | — |
| **8** | Dockerfile, compose, `DEPLOY.md`, Cloud Run mode | **Deploy to Oracle together** |

Phase 3 is the load-bearing checkpoint: it proves the frozen pipeline runs unmodified under the web
layer. Phase 4 is the riskiest (X DOM selectors), which is why it's checkpointed on real output
before any document styling work.

---

**Approve this and answer D1–D10 (or say "defaults are fine"), and I'll start at Phase 0.**
