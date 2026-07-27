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
│   ├── routes_jobs.py        submit / status / cancel / download
│   ├── jobs/
│   │   ├── store.py          SQLite job records
│   │   ├── queue.py          bounded worker pool
│   │   ├── runner.py         job dir, subprocess, progress, artifacts
│   │   └── cleanup.py        retention sweep
│   ├── templates/            server-rendered pages
│   └── static/               CSS + JS (no build step)
│
├── Dockerfile                the deployable image (Chromium included)
├── docker-compose.yml        app + Caddy (HTTPS), with persistent volumes
├── Caddyfile                 your domain, automatic HTTPS
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

**Work**

| Variable | Default | Notes |
|---|---|---|
| `WORKERS` | `3` | Browsers per job. One per 1–1.5 GB of free RAM. |
| `MAX_CONCURRENT_JOBS` | `1` | Total browsers = this × `WORKERS`. |
| `INFLUENCER_WORKERS` | `1` | Browsers for the Influencer report. Its follower-count cache is per worker process, so extra workers re-fetch the same profiles. |
| `EXECUTION_MODE` | `queue` | Background workers. `inline` exists only for hosts that stop the CPU after a response. |
| `MAX_LINKS` | `200` | Per job. |
| `MAX_UPLOAD_MB` | `5` | |
| `JOB_TIMEOUT_MINUTES` | `90` | |
| `RETENTION_DAYS` | `7` | Old jobs are deleted automatically. |

---

## Deploying on your own server

Runs on any always-on Linux box with root — a **Hostinger VPS (KVM)** is what
this is written for. Not shared/web hosting: that cannot run Chromium or Docker.

**Size it by RAM.** A browser costs ~0.5–1 GB, so allow one worker per 1–1.5 GB
free. 4 GB is a sensible floor (`WORKERS=3`); 8 GB is comfortable (`WORKERS=5`).

One container runs the web app, the background workers and Chromium. Caddy sits
in front for automatic HTTPS. Nothing else, and no external services.

### 1. Prepare the server

```bash
ssh root@<VPS_IP>
apt update && apt -y upgrade
apt -y install docker.io docker-compose-plugin git
systemctl enable --now docker
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
```

### 2. Point a domain at it

In Hostinger hPanel → DNS, add an **A record** pointing your subdomain at the
server. For this deployment that is already done:

```
report.vedictech.in  →  200.97.175.12
```

`Caddyfile` is pre-configured for that hostname.

### 3. Get the code and configure it

```bash
git clone <YOUR_REPO_URL> app && cd app
mkdir -p sessions data reports
cp .env.example .env
nano .env
```

Set at minimum:

| Variable | Value |
|---|---|
| `APP_USERS` | `alice:pw1,bob:pw2` — one pair per colleague |
| `SESSION_SECRET` | `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `X_USERNAME` / `X_PASSWORD` / `X_EMAIL` | the dedicated X capture account |
| `WORKERS` | `3` on 4 GB, `5` on 8 GB |
| `COOKIE_SECURE` | `0` for now; `1` once HTTPS works |

### 4. Seed the X login

The first sign-in is easiest done by hand, because X may show a CAPTCHA to a
brand-new server IP. On **your own computer**:

```bash
python save_login.py x                    # a browser opens; sign in
scp sessions/x_state.json root@<VPS_IP>:~/app/sessions/x_state.json
```

After that the server refreshes the cookie itself whenever it expires, using the
`X_*` credentials. If you skip this step it will simply sign in on first use.

### 5. Start it

The domain is already set in `Caddyfile`, so:

```bash
docker compose up -d --build
```

The first build takes several minutes (it pulls the Playwright image). Then:

```bash
docker compose ps            # expect "running (healthy)"
docker compose logs -f web
```

Open <https://report.vedictech.in> and sign in.

> **Testing on a bare IP instead?** Comment out the `caddy` service in
> `docker-compose.yml`, change the web service's port line to `"8000:8000"`,
> and use `http://<VPS_IP>:8000`. Keep `COOKIE_SECURE=0` while you do.

### 6. Check it

Open **X login status** in the header — it should say *Signed in*. Then run a
Twitter Report and an Influencer Report and confirm the downloads.

Reboot the server once (`reboot`) and confirm it comes back on its own —
`restart: unless-stopped` handles that.

---

## Operations and troubleshooting

| Task | Command (on the server, in `~/app`) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f web` |
| Update after a code change | `git pull && docker compose up -d --build` |
| Restart | `docker compose restart` |
| Change a password | edit `APP_USERS` in `.env`, then `docker compose up -d` |
| Rotate the X account | edit `X_USERNAME`/`X_PASSWORD`, `docker compose up -d`, then press **Sign in to X now** |
| Expired X session | handled automatically — the server signs in again |
| OS updates | `apt update && apt -y upgrade` occasionally |

| Symptom | Fix |
|---|---|
| Job dies, container restarts | out of memory — lower `WORKERS`, or add RAM |
| Captures fail on media-heavy posts | make sure `shm_size: "1gb"` is still in `docker-compose.yml` |
| Login page loops back to itself | `COOKIE_SECURE=1` without HTTPS — set `0`, or finish the Caddy step |
| "nobody can log in" | `APP_USERS` unset or malformed; needs `user:pass,user2:pass2` |
| HTTPS certificate not issued | DNS A record not pointing here yet, or ports 80/443 blocked |
| Every link hits a login wall | open **X login status**; the last sign-in error is shown there |
| Links come back `login_wall` | open **X login status**; the last sign-in error is shown there |
| Screenshot looks wrong | X may have changed its DOM — see the crop notes in `src/capture/x_capture.py` |

---

## Design notes

**The X pipeline is treated as frozen.** It was tested in production before the
web app existed, so the web layer *invokes* it rather than rewriting it. The
It is currently **byte-for-byte identical to its originally tested state** —
`git diff` against the first commit over `run.py`, `src/`, `install.py` and
`requirements.txt` is empty. A temporary two-line change existed while the app
ran on a remote browser service; that has been reverted.

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
