# Report Automation — web app

A browser front end for the X/Twitter report pipeline, plus a second report type.
Colleagues sign in, upload a list of links, pick a report type, and download the
PDF / DOCX / screenshots when the job finishes.

The original CLI still works exactly as before — see [README.md](README.md).

---

## What it adds

| | |
|---|---|
| **Login** | Username + password from `.env`. Signed httpOnly session cookie, CSRF-protected forms, login rate limiting. No OAuth. |
| **X sign-in** | The server signs in to X itself from a shared capture account, so there is no cookie file to keep alive on a host with an ephemeral disk. |
| **Upload** | `.xlsx` `.xls` `.csv` `.tsv` `.txt`, drag-and-drop. Parsed and validated *before* the job is queued, so bad files fail instantly with a clear message. |
| **Two report types** | **Twitter Report** — the existing pipeline, invoked unchanged. **Influencer Report** — new: keeps likes/reposts inside the screenshot and adds a metrics table. |
| **Background jobs** | Multiple colleagues can submit at once. Each job is isolated, with live `queued → running → done` status and a "captured 12 / 40" counter. |
| **Downloads** | PDF, DOCX, and all screenshots as one ZIP — scoped to the session that created the job. |
| **Activity log** | Every job page lists what happened, including which links could not be captured and why. |

---

## The two report types

### Twitter Report (unchanged)

The frozen pipeline, invoked exactly as `python run.py …` does. The engagement
bar is cropped out; one tweet per page; links table at the end.

### Influencer Report (new)

Same platform, separate implementation. The differences:

* the screenshot **keeps** the username, text, media **and** the visible
  likes / reposts — nothing is cropped above the engagement bar;
* **replies are captured correctly.** On a reply URL, X renders the parent tweet
  first; the influencer capture scores every article on the page and picks the
  one the link actually points at;
* video / reel posts wait for the poster frame so they do not shoot black;
* metrics are read off the page and listed under each screenshot:

| Label in the report | Read from X |
|---|---|
| **Followers** | the author's profile (one visit per account, cached) |
| **Reactions** | Likes |
| **Comments** | Replies |
| **Reach** | Views |
| **Shares** | Reposts |

Anything that cannot be read renders as `—`. Numbers use X's own compact form
(`1.2K`, `45K`).

**Layout** — A4, **two posts per page** side by side. Each post is: the account
name as a coloured heading, the screenshot in a bordered card, the five metrics
as ruled `LABEL value` rows, and a clickable `Link: <url>`. The document ends
with a single-column links table in input order with no serial numbers.

The input sheet's category is deliberately **not** printed above each post — it
is usually just a section word like "Tweet links". The account name goes there
instead.

---

## Running it locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-web.txt
.venv/bin/python -m playwright install chromium

cp .env.example .env        # then edit APP_USERS and SESSION_SECRET
python save_login.py x      # once — saves sessions/x_state.json

.venv/bin/python -m uvicorn webapp.main:app --port 8000
```

Open <http://127.0.0.1:8000>.

For the free cloud deployment (Hugging Face Spaces), see **[DEPLOY.md](DEPLOY.md)**.

---

## How the X sign-in works

Captures need a logged-in X session — engagement metrics are not visible
otherwise. The server keeps one, using a **shared, dedicated capture account**:

* set `X_USERNAME`, `X_PASSWORD` and `X_EMAIL` (X asks for the email whenever it
  sees a login from an unfamiliar machine, which a cloud server always is);
* the server signs in at startup, and again whenever a report finds the session
  missing or rejected;
* the result is written to `sessions/x_state.json` in exactly the format the
  frozen pipeline already reads, so nothing downstream changed;
* if captures hit login walls, the stale session is thrown away so the next
  report signs in fresh — **unless** no credentials are configured, in which
  case a hand-uploaded cookie file is left alone rather than destroyed.

This is what makes a host with an **ephemeral disk** usable: there is no file an
admin has to keep re-uploading after every restart. `python save_login.py x`
still works as a manual fallback.

Credentials are read from the environment only. They are never logged, never
returned by an API, and never sent to the browser. `X_TOTP_SECRET` covers an
account with 2FA, though turning 2FA off on the capture account is simpler.

---

## How the frozen pipeline stays frozen

Nothing under `run.py`, `src/`, `save_login.py`, `install.py` or
`requirements.txt` was modified — `git status` on those paths is clean.

* **Invocation.** Jobs run the CLI as a subprocess, exactly as you would by hand:
  `python run.py input.xlsx --title … --date … --workers …`.
* **Isolation.** `src/run_report.py` anchors its output with
  `Path(__file__).resolve().parents[1]` — that is the *file's* location, not the
  working directory, and `.resolve()` collapses symlinks. So changing `cwd` would
  not redirect anything and concurrent jobs would collide in one `reports/`.
  Instead each job gets its own physical copy of the code (~90 KB of `.py`,
  milliseconds), with `sessions/` symlinked to the single real cookie file.
* **Progress.** Parsed from the pipeline's own stdout — no instrumentation added.
* **Influencer path.** Entirely new files under `influencer/`. `inf_worker.py`
  imports `inf_capture` directly, so neither `src/_worker.py` nor
  `src/capture/__init__.py` needed a routing change.
* **Input quirks.** `.xls` and `.tsv` are not handled correctly by the frozen
  `input_loader` (an `.xls` fails the zip check and is read as text; a `.tsv` is
  parsed with csv's *comma* dialect). The web layer normalizes every upload into
  a canonical `.xlsx` first, so the loader itself needed no change. The layout
  logic is not reimplemented — the normalizer hands its grid to the frozen
  `input_loader._rows_from_grid`.

---

## Layout

```
webapp/                 the web layer
├── main.py             FastAPI app, pages, auth routes
├── config.py           .env loading and settings
├── auth.py             sessions, CSRF, rate limiting
├── uploads.py          upload parsing, validation, normalization
├── x_login.py          headless X sign-in for the shared capture account
├── routes_jobs.py      submit / status / cancel / download
├── jobs/
│   ├── store.py        SQLite job records
│   ├── queue.py        bounded worker pool
│   ├── runner.py       job dir, subprocess, progress, artifacts
│   └── cleanup.py      retention sweep
├── templates/          Jinja2 pages
└── static/             CSS + JS (no build step)

influencer/             the new report pipeline
├── run_influencer.py   entrypoint (mirrors run.py's CLI)
├── inf_runner.py       capture orchestration
├── inf_worker.py       parallel worker
├── inf_capture.py      capture keeping engagement + metrics
└── inf_report_builder.py   A4 PDF + DOCX

data/                   runtime state (gitignored)
└── jobs/<job_id>/      one isolated folder per job
```

---

## Configuration

All settings live in `.env` — see [`.env.example`](.env.example) for the
documented list. The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `APP_USERS` | — | `alice:pw1,bob:pw2`. A bcrypt hash is used automatically if the value starts with `$2`. |
| `SESSION_SECRET` | — | Long random string. Changing it signs everyone out. |
| `MAX_CONCURRENT_JOBS` | `1` | Jobs running at once. |
| `CAPTURE_WORKERS` | `4` | Browsers per job. Memory ≈ jobs × workers × 0.7 GB. |
| `MAX_LINKS` | `200` | Per job. |
| `RETENTION_DAYS` | `7` | Old job folders are deleted automatically. |
| `EXECUTION_MODE` | `queue` | `inline` for scale-to-zero hosts (see DEPLOY.md). |
| `X_USERNAME` / `X_PASSWORD` / `X_EMAIL` | — | The shared X capture account the server signs in with. |
| `X_TOTP_SECRET` | — | Only if that account has 2FA on. |
| `COOKIE_SECURE` | `0` | Set to `1` when served over HTTPS (Hugging Face always is). |

---

## Security notes

* `sessions/x_state.json` and `.env` are never served, never sent to the browser,
  and never baked into the Docker image (mounted as a read-only volume).
* Uploaded filenames are display-only; the report name is sanitized to
  `[A-Za-z0-9._-]` before it touches the filesystem.
* Downloads are resolved through the job record and re-checked to be inside that
  job's `out/` folder, then matched against the session's owner.
* Upload size and link count are both capped, and files must parse before a job
  is created.
