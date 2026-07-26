# X / Twitter Report Automation

Give it a list of X/Twitter post links; it screenshots every post in a logged-in
browser and builds a **PDF + Word (.docx)** report.

> Changing or rebuilding this project? Read **[RULEBOOK.md](RULEBOOK.md)** first —
> it collects the constraints and traps that are not obvious from the code.

Two ways to use it:

* **Web app** — colleagues sign in, upload a links file, pick a report type,
  download the result. This is the main interface.
* **CLI** — the original command-line tool, unchanged.

Two kinds of report:

| | **Twitter Report** | **Influencer Report** |
|---|---|---|
| Screenshot | tweet with the engagement bar **cropped out** | keeps username, text, media **and** likes/reposts |
| Metrics | none | Followers, Reactions, Comments, Reach, Shares |
| Layout | one post per page, letter | **two posts per page**, A4 |
| Ends with | links table | links table |

---

## Table of contents

1. [Quick start (local)](#quick-start-local)
2. [How it works](#how-it-works)
3. [Project structure](#project-structure)
4. [The input file](#the-input-file)
5. [Configuration](#configuration)
6. [Deploying (free, no credit card)](#deploying-free-no-credit-card)
7. [Operations and troubleshooting](#operations-and-troubleshooting)
8. [Design notes](#design-notes)

---

## Quick start (local)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-web.txt
.venv/bin/python -m playwright install chromium

cp .env.example .env          # set APP_USERS and SESSION_SECRET
python save_login.py x        # one-time: log into X by hand in the window that opens

.venv/bin/python -m uvicorn webapp.main:app --port 8000
```

Open <http://127.0.0.1:8000>.

Locally the defaults apply: a **local Chromium** and a **background job queue**.

### The CLI, if you prefer it

```bash
python run.py config/links.xlsx --title "Twitter Report"    # Twitter report
python influencer/run_influencer.py links.xlsx              # Influencer report
python run.py -                                             # paste links on stdin
```

Flags: `--title`, `--date dd-mm-yy`, `--workers N`, `--headed`. Output lands in
`reports/`.

---

## How it works

```
 Browser                    App (FastAPI)                     Browser engine
 ───────                    ─────────────                     ──────────────
 sign in  ──────────────▶  session cookie, CSRF, rate limit
 upload file ───────────▶  parse + validate + normalise to .xlsx
 Generate ──────────────▶  create job, isolated working dir
                                  │
                                  ├─ subprocess: run.py  or  run_influencer.py
                                  │        │
                                  │        └── Playwright ──▶ Chromium
                                  │              (local, or remote over CDP)
                                  │
                           progress parsed from the pipeline's stdout
 live status ◀──────────  NDJSON stream / polled JSON
 download   ◀──────────  PDF · DOCX · screenshots.zip
```

**The process, step by step:**

1. **Upload.** Any of `.xlsx .xls .csv .tsv .txt`. The web layer parses it,
   validates that it contains X links, counts them, and rewrites it as a
   canonical `.xlsx`. Bad files are rejected here, before a job exists.
2. **Job creation.** Each submission gets its own directory under `data/jobs/`
   containing a private copy of the pipeline code, so concurrent jobs cannot
   collide.
3. **X sign-in.** Before capture, the app makes sure a valid X session exists —
   restoring it from the session store, or signing in headlessly with the shared
   capture account.
4. **Capture.** The pipeline runs as a subprocess, exactly as the CLI does. It
   screenshots each post, retries failures, and re-captures blank/black shots.
5. **Build.** Screenshots are JPEG-compressed and assembled into a PDF and DOCX.
6. **Deliver.** PDF, DOCX and a ZIP of all screenshots, scoped to the session
   that created the job. Links that failed are listed in the activity log with a
   reason, and left out of the document.

---

## Project structure

```
├── run.py                    CLI entrypoint — Twitter report
├── save_login.py             one-time manual X login
├── install.py                dependency + browser setup
│
├── src/                      the X capture pipeline (frozen — see Design notes)
│   ├── run_report.py         orchestration: workers, retries, quality pass
│   ├── _worker.py            one browser per worker process
│   ├── input_loader.py       reads .xlsx / .csv / pasted lists
│   ├── platforms.py          X constants + login helpers
│   ├── shot_quality.py       detects blank / black / half-loaded screenshots
│   ├── report_builder.py     builds the Twitter PDF + DOCX
│   ├── save_sessions.py      manual login flow
│   ├── browser_backend.py    local Chromium vs remote browser over CDP
│   └── capture/x_capture.py  the X capture algorithm
│
├── influencer/               the Influencer report (parallel to src/)
│   ├── run_influencer.py     CLI entrypoint
│   ├── inf_runner.py         orchestration
│   ├── inf_worker.py         parallel worker
│   ├── inf_capture.py        capture keeping engagement + reading metrics
│   └── inf_report_builder.py A4 PDF + DOCX, two posts per page
│
├── webapp/                   the web layer
│   ├── main.py               app, pages, auth routes
│   ├── config.py             environment / .env settings
│   ├── auth.py               sessions, CSRF, login rate limiting
│   ├── uploads.py            upload parsing, validation, normalisation
│   ├── x_login.py            headless X sign-in for the shared account
│   ├── x_state_store.py      mirrors the X session to a private GitHub repo
│   ├── routes_jobs.py        submit / status / cancel / download
│   ├── jobs/
│   │   ├── store.py          SQLite job records
│   │   ├── queue.py          bounded worker pool
│   │   ├── runner.py         job dir, subprocess, progress, artifacts
│   │   └── cleanup.py        retention sweep
│   ├── templates/            server-rendered pages
│   └── static/               CSS + JS (no build step)
│
├── Dockerfile                the deployable image (no browser inside — see below)
├── render.yaml               Render blueprint
├── requirements.txt          CLI dependencies
├── requirements-web.txt      web app dependencies
│
├── config/links.xlsx         default input list
├── sessions/x_state.json     the X login cookie — SECRET, gitignored
├── reports/                  CLI output
└── data/                     web app runtime state — gitignored
```

---

## The input file

Accepted: `.xlsx`, `.xls`, `.csv`, `.tsv`, `.txt`.

* A column of X/Twitter post URLs. If a header row names the columns
  (`link` / `url` / `post link`, optionally `account` / `handle` / `category`),
  those are used; otherwise any cell containing a URL is treated as a link.
* A non-URL row becomes a section heading (category) for the links beneath it.
* Non-X links are skipped with a note.

Order is preserved throughout — the document follows the file.

---

## Configuration

Everything is environment variables, loaded from `.env` locally. See
[`.env.example`](.env.example) for the full annotated list.

**App access**

| Variable | Default | Notes |
|---|---|---|
| `APP_USERS` | — | `alice:pw1,bob:pw2`. A bcrypt hash is detected automatically. |
| `APP_USER` / `APP_PASS` | — | Alternative single-account form. |
| `SESSION_SECRET` | — | Long random string. Changing it signs everyone out. |
| `COOKIE_SECURE` | `0` | Set `1` when served over HTTPS. |

**The shared X capture account** — lets the server sign in by itself

| Variable | Notes |
|---|---|
| `X_USERNAME` | the @handle, without the `@` |
| `X_PASSWORD` | its password |
| `X_EMAIL` | X asks for this on logins from unfamiliar machines |
| `X_TOTP_SECRET` | only if that account has 2FA on |

**Browser**

| Variable | Default | Notes |
|---|---|---|
| `BROWSER_BACKEND` | `local` | `local` launches Chromium here; `browserless` connects to a remote one. |
| `BROWSERLESS_WS` | — | `wss://<region>.browserless.io?token=…` |
| `BROWSER_MAX_CONCURRENCY` | `2` | Total simultaneous browsers is clamped to this. |

**Work**

| Variable | Default | Notes |
|---|---|---|
| `EXECUTION_MODE` | `queue` | `queue` = background workers; `inline` = capture runs inside the request (needed on hosts that sleep). |
| `MAX_CONCURRENT_JOBS` | `1` | |
| `CAPTURE_WORKERS` | `4` | Browsers per job (Twitter report). |
| `INFLUENCER_WORKERS` | `1` | Browsers per job (Influencer report). Kept at 1 so the follower-count cache is shared and the browser plan's concurrency limit has headroom. |
| `MAX_LINKS` | `200` | Per job. |
| `MAX_UPLOAD_MB` | `5` | |
| `JOB_TIMEOUT_MINUTES` | `90` | |
| `RETENTION_DAYS` | `7` | Old jobs are deleted automatically. |

**Session persistence** (for hosts with no disk)

| Variable | Notes |
|---|---|
| `X_STATE_STORE` | `none` or `github` |
| `X_STATE_GITHUB_REPO` | `you/report-secrets` — must be **private** |
| `X_STATE_GITHUB_TOKEN` | fine-grained PAT, Contents: read+write, that repo only |

---

## Deploying (free, no credit card)

**Render** runs the app; **Browserless** runs the browser. Neither needs a card.

```
Office user ──HTTPS──▶ Render free web service : the app (no browser inside)
                          ├──WebSocket (CDP)──▶ Browserless : runs Chromium
                          └──────────────────▶ private GitHub repo : x_state.json
```

The image ships **without a browser** — 536 MB instead of 4 GB — which is what
makes it fit a 512 MB free host.

### Measured limits — read before you start

| | |
|---|---|
| App memory, 5-link report | 366 MB |
| App memory, 20-link report | 413 MB |
| **Render free tier** | **512 MB — the binding constraint** |
| Browserless free | ~500 browser-minutes/month, **2 concurrent browsers** |
| Render free | sleeps after ~15 min idle, ~30–50 s cold start, no disk |

**Keep reports to about 25 links.** A 40-link report would very likely be killed
for using too much memory; split larger lists. At ~3–4 browser-minutes per
20-link report, budget roughly 100–150 reports a month.

### Steps

**1. Browserless.** Sign up at <https://www.browserless.io> (free, no card).
Copy the API token and region; your connection string is
`wss://production-sfo.browserless.io?token=YOUR_TOKEN`.

**2. A private repo for the X session.** Render wipes its disk on every restart,
so without this the app re-signs-in to X on every cold start and burns
Browserless minutes. Create a **private** repo (e.g. `report-secrets`) and a
[fine-grained PAT](https://github.com/settings/tokens?type=beta) scoped to only
that repo with **Contents: read and write**.

> That file is a live X session — whoever holds it can act as the capture
> account. Private repo, repo-scoped token.

**3. Push the code** to GitHub.

**4. Create the Render service.** <https://render.com> → **New → Web Service** →
this repo → Runtime **Docker**, Instance type **Free**, Health check path
`/health`. *(Or **New → Blueprint**, which reads [`render.yaml`](render.yaml).)*

**5. Environment variables.** In the service → Environment:

```
BROWSER_BACKEND=browserless
BROWSERLESS_WS=wss://production-sfo.browserless.io?token=YOUR_TOKEN
BROWSER_MAX_CONCURRENCY=2

EXECUTION_MODE=inline
MAX_CONCURRENT_JOBS=1
CAPTURE_WORKERS=2
MAX_LINKS=25
JOB_TIMEOUT_MINUTES=25

APP_USERS=alice:pw1,bob:pw2
SESSION_SECRET=<python3 -c "import secrets;print(secrets.token_urlsafe(48))">
COOKIE_SECURE=1

X_USERNAME=yourhandle
X_PASSWORD=itspassword
X_EMAIL=its@email.com

X_STATE_STORE=github
X_STATE_GITHUB_REPO=you/report-secrets
X_STATE_GITHUB_TOKEN=github_pat_…
```

**6. Check it.** Open the URL, sign in, and look at **X login status** in the
header — it should say *Signed in*. Run a small report of each type and confirm
the downloads. **Download immediately** — the page says so, because Render's
disk does not survive sleep.

**7. Optional: fewer cold starts.** Point a free uptime pinger at
`https://<name>.onrender.com/health` every 10 minutes during office hours.

**8. Watch usage.** The Browserless dashboard shows units consumed
(1 unit ≈ 30 s of browser time).

> **Use a dedicated X account for capturing**, never a personal one — bulk
> captures can get an account rate-limited or suspended. Turn 2FA off on it if
> you can.

### Hosts that do not work

Vercel and serverless functions (cannot run a minutes-long backend), Hugging
Face free Spaces (Docker requires a paid plan since July 2026), Railway (trial
credits only). Cloud Run, Fly and Oracle all work but require a credit card.

---

## Operations and troubleshooting

| Task | How |
|---|---|
| Update the app | `git push` — Render redeploys |
| Logs | Render dashboard → Logs |
| Change a password | edit `APP_USERS` in Environment |
| Rotate the X account | edit `X_USERNAME`/`X_PASSWORD`, then press **Sign in to X now** |
| Expired X session | handled automatically — signs in again and re-saves |

| Symptom | Fix |
|---|---|
| "Executable doesn't exist" | `BROWSER_BACKEND` / `BROWSERLESS_WS` not set — the image has no browser |
| `connect_over_cdp … 401` | wrong Browserless token |
| `connect_over_cdp … 429` | more than 2 concurrent browsers — keep jobs×workers ≤ 2 |
| Job dies, service restarts | out of memory — lower `MAX_LINKS` |
| First request takes ~40 s | it was asleep; set up the keep-alive ping |
| Signs in to X every report | `X_STATE_STORE` unset, or the PAT lacks Contents:write |
| Login page loops | `COOKIE_SECURE=1` without HTTPS |
| "nobody can log in" | `APP_USERS` unset or malformed |
| Links come back `login_wall` | open **X login status**; the last sign-in error is shown there |
| Screenshot looks wrong | X may have changed its DOM — see the crop notes in `src/capture/x_capture.py` |

---

## Design notes

**The X pipeline is treated as frozen.** It was tested in production before the
web app existed, so the web layer *invokes* it rather than rewriting it. The
only change ever made to it is two lines in `src/_worker.py` (an import, and
`p.chromium.launch(...)` → `launch_browser(p, headless)`) so it can use a remote
browser. With `BROWSER_BACKEND` unset that helper returns exactly
`p.chromium.launch(headless=…)`, so the original behaviour is byte-for-byte
intact.

**Job isolation copies code, not just cwd.** `src/run_report.py` anchors its
output with `Path(__file__).resolve().parents[1]` — the *file's* location, not
the working directory, and `.resolve()` collapses symlinks. Changing `cwd` would
therefore not redirect anything, and concurrent jobs would collide in one
`reports/`. So each job gets its own physical copy of the code (~90 KB, a few
milliseconds) with `sessions/` symlinked to the one real cookie.

**Progress comes from stdout.** The pipeline already prints what it is doing, so
the runner parses those lines rather than instrumenting the pipeline.

**Uploads are normalised before use.** The frozen `input_loader` mishandles two
formats — an `.xls` fails the zip check and gets read as text, and a `.tsv` is
parsed with csv's *comma* dialect so the link ends up glued to the account name.
The web layer converts every upload to a canonical `.xlsx` first, which fixes
both without touching the loader. The layout logic is not reimplemented: the
normaliser hands its grid to the frozen `input_loader._rows_from_grid`.

**The Influencer report is a parallel implementation**, not a fork. It mirrors
the proven structure but crops *below* the engagement bar, picks the correct
article on reply pages (where the first article is the parent tweet, not the one
you linked), waits for video poster frames, and reads metrics from the action
bar's `aria-label`. `inf_worker.py` imports `inf_capture` directly, so neither
`src/_worker.py`'s routing nor the capture dispatcher needed to change.

**Why the browser is remote in production.** Free hosts without a credit card
cap RAM near 512 MB, which cannot run Chromium at all. All capture logic
operates on a Playwright `page`, so it is identical whether the browser is local
or reached over CDP — only where the browser lives changes.

**Security.** `sessions/x_state.json` and `.env` are never served, never sent to
the browser, and never baked into the image. Uploaded filenames are display-only;
the report name is sanitised before touching the filesystem. Downloads are
resolved through the job record, re-checked to be inside that job's folder, and
matched against the session owner. Upload size and link count are capped, and
files must parse before a job is created.
