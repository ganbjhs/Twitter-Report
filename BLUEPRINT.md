# BLUEPRINT — X (Twitter) Report Automation

This single file specifies the **entire** project. If the code were deleted, you
could rebuild it faithfully from this document alone. It describes what each
piece does, the exact algorithms and thresholds, the data contracts between
pieces, and how they are wired together.

---

## 1. What it does (one paragraph)

Given a list of **X / Twitter post links** (an Excel sheet or a pasted list),
the tool opens each post in a **logged-in Chromium** (via Playwright), takes
**one clean screenshot of the tweet** — cropped to `header → text → media`, with
the engagement bar, view/like counts and the surrounding "your account" UI
excluded — verifies each shot actually rendered (not black / blank / half-loaded
/ behind a sensitive-content gate, recapturing the bad ones), and assembles a
**PDF and a Word (.docx)** report: a dated header, one tweet per page (screenshot
centered at the top, link left-aligned beneath), and a final links table.
Failed links are skipped. Screenshots are JPEG-compressed for a small file.
Output is named by date and saved to `reports/`.

---

## 2. Pipeline (data flow)

```
Excel .xlsx  ── or ──  pasted list (stdin)
        │   input_loader.load()  → rows[{category, account_name, link, post_link, platform}]
        ▼
   run_report.build_tasks()  → tasks[{idx, capture_url, post_link, account, category, shot}]
        │   split round-robin across N worker processes
        ▼
   _worker.run_chunk()  → one Playwright browser per process
        │      capture.capture(page, url, shot) → x_capture.capture()
        ▼
   x_capture:  robust load (retry transient) → reveal sensitive → wait for media
               → crop above engagement bar → screenshot → inline quality retake
        ▼
   run_report:  retry pass (failed shots) → QUALITY recapture pass (bad shots)
                → verify → write reports/results.json
        ▼
   report_builder:  keep only good shots → JPEG-compress → build PDF + DOCX
        ▼
   reports/<Title>_<date>.pdf  +  .docx   (+ reports/screenshots/*.png, results.json)
```

---

## 3. Stack & dependencies

- **Python 3.9+** (developed on 3.13).
- **playwright** (`>=1.40,<2.0`) — drives Chromium for login + capture.
- **reportlab** (`>=4.0,<5.0`) — builds the PDF. (Pulls in **Pillow**, reused
  for screenshot quality analysis + JPEG compression.)
- **python-docx** (`>=1.1,<2.0`) — builds the DOCX.
- **openpyxl** (`>=3.1,<4.0`) — reads `.xlsx`.

`install.py` does: `pip install -r requirements.txt`, then
`playwright install chromium` (+ `install-deps chromium` on Linux), then creates
`config/`, `sessions/`, `reports/screenshots/`.

---

## 4. File structure

```
report-automation/
├── install.py            # one-command setup (deps + Chromium + folders)
├── run.py                # entrypoint: capture + verify + build report
├── save_login.py         # thin wrapper → src/save_sessions.py (save X login)
├── requirements.txt
├── README.md  /  BLUEPRINT.md
├── config/
│   └── links.xlsx        # the input list (default source)
├── sessions/             # x_state.json (login cookies) — SECRET, gitignored
├── reports/              # <Title>_<date>.pdf + .docx, results.json, screenshots/
└── src/
    ├── run_report.py     # orchestration: tasks, parallelism, retries, quality, verify
    ├── _worker.py        # one Playwright browser per worker process
    ├── input_loader.py   # read .xlsx / pasted list → normalized rows
    ├── platforms.py      # X constants + login helpers
    ├── shot_quality.py   # detect black/blank/half-loaded screenshots
    ├── report_builder.py # build the PDF + DOCX
    ├── save_sessions.py  # manual login flow → sessions/x_state.json
    └── capture/
        ├── __init__.py   # dispatcher (X-only)
        └── x_capture.py  # the capture algorithm
```

---

## 5. Data contracts

**Input row** (from `input_loader`):
```python
{"category": str, "account_name": str, "link": str, "post_link": str, "platform": "x"}
```

**Task** (from `run_report.build_tasks`):
```python
{"idx": int, "capture_url": str, "post_link": str, "account": str,
 "category": str, "platform": "x", "shot": "<abs path>.png"}
```

**Capture result** (from `x_capture.capture`, stamped by worker):
```python
{"url", "status", "handle", "screenshot", "text", "platform",
 "idx", "category", "account_name", "post_link"}
# status ∈ {"ok", "login_wall", "not_found", "error: …"}
```
`results.json` is a list of these (idx removed, sorted to input order).

---

## 6. Module specs

### platforms.py
X-only. `PLATFORMS = {"x": {domains:("x.com","twitter.com"), login_url,
home_url, auth_cookie:"auth_token", login_hints}}`. `platform_of(url)` always
returns `"x"`. `domain_matches(value, platform="x")` = any domain substring in
value.

### input_loader.py
`load(source)`:
- `source in {"-","paste","stdin"}` → `load_stdin()` (read stdin, parse as list).
- else → `load_excel(source)`.

`load_excel(path)`: open with openpyxl (`read_only, data_only`); on
`BadZipFile`/`InvalidFileException` (a text file mis-named `.xlsx`) fall back to
`load_delimited(path)`. Turn the sheet into a grid of trimmed strings →
`_rows_from_grid`.

`load_delimited(text_or_path)`: parse with `csv.reader` (handles both CSV and
one-link-per-line) → `_rows_from_grid`.

`_rows_from_grid(grid)`: drop empty rows, then:
- **Table layout** if a header row within the first 5 rows names a link column
  (`link/url/post link/post_link/tweet/tweet link/tweet url`). Optional columns:
  account (`account/account name/name/handle/page name/author`), category
  (`category/section/group`). Read each data row's link cell (or any URL cell).
- **Plain-list layout** otherwise: any cell/line with an `http(s)://` URL is a
  link; a URL-less line becomes the running **category** header; lines starting
  with `#` are comments.

Each link → `_row(category, account, link)`:
- `_clean_url`: strip whitespace and trailing punctuation `.,;:!?)]}>"'`
  (so a stray `.../123.` pasted from a sheet becomes `.../123`).
- `account` defaults to `derive_name(url)`: `@handle` from
  `x.com/<handle>/status/…`, else `"X post"` (for `/i/status/…` links whose
  handle is hidden — the capture reads the real handle off the page).
- `platform` = `"x"`.

Finally keep only X links (`is_x_url`: contains `x.com` or `twitter.com`);
print how many non-X links were dropped.

### save_sessions.py  (and save_login.py → its `main()`)
`python src/save_sessions.py x` opens a **real Chrome** window (persistent
context at `sessions/.chrome-login-x`, `channel="chrome"`, falling back to
bundled Chromium) with automation flags removed
(`ignore_default_args=["--enable-automation"]`,
`--disable-blink-features=AutomationControlled`, and an init script hiding
`navigator.webdriver`) on `https://x.com/login`. It polls until the `auth_token`
cookie appears, waits ~2.5 s for secondary cookies to settle, then writes
`sessions/x_state.json` (Playwright `storage_state`, filtered to X cookies only).
This file is a **secret** (gitignored).

### capture/__init__.py
`capture(page, url, shot_path, platform="x")` → calls `x_capture.capture(...)`,
stamps `result["platform"]="x"`, returns it.

### capture/x_capture.py  — the capture algorithm
Selectors/consts:
```
TWEET_SELECTOR   = 'article[data-testid="tweet"]'
NOT_FOUND_PHRASES = [this post is unavailable, post unavailable, this post was
   deleted, hmm...this page doesn't exist, doesn't exist, account doesn't exist,
   has been suspended, account suspended, posts are protected, no longer available]
TRANSIENT_PHRASES = [something went wrong, try reloading, rate limit]
_LOAD_ATTEMPTS = 3 ; _SELECTOR_TIMEOUT = 22000
_METADATA_LOOKBACK = 260 ; _TOP_PAD = 2
_MEDIA_TIMEOUT = 10000 ; _IDLE_TIMEOUT = 3500
_SHOOT_RETRIES = 2
```

`capture(page, url, shot_path)` returns `{"url","status","handle","screenshot","text"}`:

1. **`_load_tweet(page, url)`** → `"ok"|"login_wall"|"not_found"`. Loop up to
   `_LOAD_ATTEMPTS`: `goto(url, domcontentloaded, 45s)`, then
   `wait_for_selector(TWEET_SELECTOR, 22s)` → `"ok"`. If no tweet: read visible
   body text + html; if login button/"sign in to x" → `login_wall`; if any
   `NOT_FOUND_PHRASES` → `not_found`; if any `TRANSIENT_PHRASES` → click a
   "Retry" button if present (else reload) and continue the loop. Otherwise wait
   1.5 s and retry. After all attempts → `not_found`. **This is what stops X's
   transient "Something went wrong" from being falsely flagged not_found.**
2. If not `"ok"`: save a full-page screenshot as evidence, return.
3. Locate the first `article[data-testid="tweet"]`. Remove any logged-out
   bottom banner / dialog overlapping it.
4. **`_reveal_sensitive(page, tweet)`**: click any button labelled exactly
   `"View"` or `"Show"` inside the tweet (X's sensitive-content gate) so the
   media is visible; wait ~0.8 s after each.
5. **`_wait_rendered(page, tweet)`**: scroll tweet into view; brief
   `networkidle` wait (`_IDLE_TIMEOUT`, X long-polls so this is only a nudge);
   then `wait_for_function` until every `<img>` in the tweet is
   `complete && naturalWidth>0` and no `[role="progressbar"]`/`[aria-label=Loading]`
   remains (`_MEDIA_TIMEOUT`); 0.5 s settle. Each step swallows its own timeout
   so a stuck asset can't hang the run.
6. `handle = _read_handle(tweet)`: the `@…` token from `[data-testid="User-Name"]`.
7. **Crop + shoot** via `_crop_box(page, tweet)`: take the article's bounding
   box; the cut line = the **highest** of (a) the top of the last action
   `[role="group"]` and (b) a metadata `<time>` whose bottom sits within
   `_METADATA_LOOKBACK` px above that group. Clip from the article top down to
   the cut (minus `_TOP_PAD`). This removes the engagement/action bar, the
   `time · views` line and the aggregate counts, while a quoted tweet's far-above
   timestamp is ignored. Screenshot the clip (fallback: whole article element).
8. **Inline quality retake**: check the shot with `shot_quality`; while it looks
   bad, wait 2 s, re-`_reveal_sensitive`, re-`_wait_rendered`, re-shoot — up to
   `_SHOOT_RETRIES` times.
9. Read `text` from `[data-testid="tweetText"]` (best-effort); brief random pace.

### shot_quality.py
`screenshot_quality(path) → (good: bool, reason: str)` using Pillow:
- Can't import Pillow → `(True, "pil-missing")` (don't block).
- Open as grayscale; `w<150 or h<180` → `(False, "too-small …")`.
- `stddev < 8` → `(False, "blank-or-uniform")` (all-black / all-white / spinner).
- `mean < 25 and stddev < 18` → `(False, "too-dark")`.
- else `(True, "ok")`.
Rationale: a real tweet always has a light header band with dark text, so it has
high pixel variance; failed captures collapse toward a single color.

### _worker.py
`run_chunk(chunk, headless, storage_state, ctx_kwargs, src_path)`: put `src` on
path; launch one `chromium` browser; new context with `ctx_kwargs` + optional
`storage_state`; one page; for each task call `capture.capture(...)`, catch
exceptions into `status="error: …"`, stamp `idx/category/account_name/post_link`;
return results. Kept importable so it pickles under macOS `spawn`.

### run_report.py  — orchestration
`CTX_KWARGS`: `viewport 1280×1600`, `locale en-IN`, a desktop Chrome UA.
`MIN_SHOT_BYTES = 1024`. `DEFAULT_WORKERS = 3`.

- `resolve_source(argv)`: first positional arg (an `.xlsx` path or `-`), else
  `config/links.xlsx`.
- `x_storage_state()`: load `sessions/x_state.json` cookies/origins, else warn
  and run logged-out.
- `build_tasks(rows)`: number links from 1; `shot = reports/screenshots/{idx:02d}_{safeaccount}.png`.
- `main()`:
  1. `input_loader.load(resolve_source())` → tasks.
  2. Split tasks round-robin into `min(workers, len)` chunks; run each chunk in a
     `ProcessPoolExecutor` (or inline if one chunk).
  3. **Retry pass**: any result failing `_shot_ok` (status ok + file exists +
     `>MIN_SHOT_BYTES`) is recaptured once, sequentially.
  4. **Quality recapture pass**: any `_shot_ok` result that fails `_quality_ok`
     (`shot_quality` says blank/black/half-loaded) is recaptured once; print
     `[quality] improved N/M`. (Recapture overwrites the same file, so always
     take the fresh attempt.)
  5. Sort to input order, drop `idx`, `verify()` (print clean/failed tally),
     write `reports/results.json`.

### report_builder.py  — PDF + DOCX
`main(title, stem)` (argv): read `results.json`, keep only `_usable` (status ok
+ file exists + non-empty), count skipped. In a `TemporaryDirectory`,
`_compress_for_embed` converts every kept screenshot to JPEG (Pillow,
`quality=88, optimize=True, subsampling=0` — full chroma keeps text crisp,
~3–4× smaller), tagging each with pixel `_dim`. Then build both files to
`reports/<stem>.pdf` and `reports/<stem>.docx`; print resulting MB.

Shared: `_has_categories`, `_shown_link` (blank for `file://`),
`_fit(iw,ih,max_w,max_h)` (aspect-preserving scale).

**Layout (identical in both formats):**
1. **Header** at the very top: the `title` (e.g. `"Twitter Report 25-07-26"`),
   centered bold ~18pt, with a horizontal **separator rule** beneath (PDF:
   `HRFlowable`; DOCX: paragraph bottom-border).
2. **Screenshots**, one tweet per page: the category label (only if any link has
   a real category) + the screenshot, **centered at the top**; the **link
   left-aligned** beneath (`"Link — <url>"`, url is a real hyperlink). The
   **first** tweet sits under the header on page 1; every later tweet starts a
   new page (PDF: `PageBreak`; DOCX: a new `WD_SECTION.NEW_PAGE` section, vAlign
   top). No separate username line. Image capped to `4.9in × 7.0in`.
3. **Links list**, flowing **right below the last screenshot block (no page
   break)**: a single column headed **"Link"** whose header text is **blue**
   (`#1D9BF0`) on a light-blue fill (`#D9E8F5`); one row per link with the
   clickable URL.

### run.py  — entrypoint
Parse & pop `--title` (default `"Twitter Report"`) and `--date` (default
`today` as `dd-mm-yy`). `header = f"{title} {date}"`;
`stem = re.sub(r"[^0-9A-Za-z._-]+","_", f"{title}_{date}")`. Set `sys.argv` and
call `run_report.main()` (capture+verify), then
`report_builder.main()` with `[header, stem]`.

---

## 7. CLI

```bash
python install.py                        # once: deps + Chromium + folders
python save_login.py x                   # once: log into X by hand (saves cookies)
python run.py config/links.xlsx          # capture + verify + build report
python run.py config/links.xlsx --workers 8 --date 25-07-26
python run.py -                          # paste links on stdin, Ctrl-D
python run.py --headed                   # watch the browser
```
Output: `reports/Twitter_Report_<date>.pdf` and `.docx`.

---

## 8. Config, secrets, gitignore

- Input: `config/links.xlsx` (Excel, or a text file of one link per line — the
  loader tolerates either).
- Secret: `sessions/x_state.json` (live login cookies) — never commit;
  `sessions/` is gitignored, along with `reports/*.pdf|.docx`,
  `reports/results.json`, `reports/screenshots/`, `__pycache__/`, `.DS_Store`.

---

## 9. Rebuild checklist

1. Create the file tree in §4; write `requirements.txt` (§3) and `install.py`.
2. `platforms.py` → `input_loader.py` → `shot_quality.py` (no cross-deps beyond
   `platforms`).
3. `capture/x_capture.py` (§6 algorithm) + `capture/__init__.py`; `_worker.py`.
4. `run_report.py` (tasks, parallelism, retry + quality passes, verify).
5. `report_builder.py` (compress + PDF/DOCX layout in §6).
6. `run.py` (flags → header/stem → run_report → report_builder);
   `save_sessions.py` + `save_login.py`.
7. `.gitignore` (§8). Verify: `install.py`, `save_login.py x`,
   `run.py config/links.xlsx`.
```
