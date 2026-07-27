# Rulebook — things to know before changing this project

Every rule here comes from something that actually broke. If you are redesigning
or extending this project, read this first; it will save you the day it cost the
first time.

[README.md](README.md) explains *what the project is and how to run it*. This
file explains *what will bite you*.

---

## 1. The golden rule: the X pipeline is frozen

`run.py`, `src/run_report.py`, `src/report_builder.py`, `src/capture/`,
`src/input_loader.py` and friends were tested in production before the web app
existed. **Invoke them; do not rewrite them.**

`src/` is currently **byte-for-byte identical to its originally tested state**.
Confirm that before you ship anything:

```bash
git diff <first-commit> -- run.py src/ install.py requirements.txt   # must be empty
```

(A two-line change lived in `src/_worker.py` while the browser ran off-box. It
was approved at the time and has since been reverted.)

Before you edit anything under `src/`, ask whether you can get the same result
from outside it. You almost always can — see rules 2 and 8 for two cases where
that looked impossible and wasn't.

---

## 2. Isolation copies code; cwd does nothing

```python
ROOT = Path(__file__).resolve().parents[1]     # src/run_report.py
```

Output paths are anchored to **where the file lives**, not the working
directory — and `.resolve()` collapses symlinks, so symlinking `src/` into a job
folder resolves straight back to the original.

**Consequence:** `cwd=` will not redirect output, and two concurrent jobs would
write into the same `reports/`. Each job therefore gets its own *physical copy*
of the code (~90 KB, milliseconds) in `data/jobs/<id>/app/`.

If you ever "optimise" that copy away, concurrent jobs will silently corrupt
each other's output. The copy is the isolation.

---

## 3. Verify by looking at the artefact, not the status

The most expensive mistake in this project: the Browserless captures were
verified by checking job status (`done`), metrics (real follower counts), and
file sizes (240 KB PDFs). All green. Every screenshot was visually broken —
X's navigation sidebar was in every image.

**Rule: for anything that produces an image or a document, open it.** A green
status only proves the code did not raise.

---

## 4. Remote browsers (CDP) lie about the viewport

> Historical — the app runs a **local Chromium** now. Keep this if you ever move
> the browser off-box again; it cost a full day the first time.

Connecting over CDP is not the same as launching locally.

* `new_context(viewport=…)` is **ignored**. So is `page.set_viewport_size()`.
  Playwright reports the size you asked for while `window.innerWidth` stays at
  the service's default (800 px on Browserless).
* The only thing that works is the service's own launch option:
  `?launch={"defaultViewport":{"width":1500,"height":1600}}`.

**Why it matters:** at 800 px X switches to its narrow layout and paints the
left nav *over* the tweet column. The nav then sits inside the article's
bounding box, so no amount of cropping can remove it. The article moves from
`x=393` to `x=126` — that number is the tell.

**Debug recipe** when a remote capture looks wrong:

```python
page.evaluate("() => ({inner: innerWidth, outer: outerWidth, dpr: devicePixelRatio})")
```

If `inner` is not what you set, nothing downstream will be right.

---

## 5. Chromium ≠ Chrome: codecs

> Also historical, and also the reason a remote browser is painful.

Open-source **Chromium has no H.264/AAC**. X's videos are H.264, so every video
post renders "The media could not be played" instead of a poster frame.

* Browserless default endpoint → Chromium → broken video
* `…browserless.io/chrome?token=…` → real Chrome → works
* Playwright's bundled Chromium *does* include the codecs, which is why it
  always worked locally and only broke in the cloud

**Probe before blaming the site:**

```python
page.evaluate("""() => document.createElement('video')
    .canPlayType('video/mp4; codecs="avc1.42E01E"')""")   # "" means no codec
```

---

## 6. X's DOM: the four things that are not obvious

1. **On a reply URL, the first `article` is the PARENT tweet.** Selecting
   `.first` screenshots the wrong post. Score the articles instead: the focused
   one has no `<time>` inside `[data-testid="User-Name"]` (ancestors do), and
   its action bar reports view counts.
2. **Engagement is `[role="group"]`.** Crop *above* its top for the Twitter
   report, *below* its bottom for the Influencer report. That one boundary is
   the entire difference between the two reports.
3. **Metrics live in an `aria-label`**, not in the visible text:
   `"12 replies, 3 reposts, 45 likes, 6 bookmarks, 7890 views"`. One parse gets
   everything; the per-button fallbacks are only for when that is missing.
4. **Follower count is not on the post.** It requires a profile visit, so it is
   cached per handle — and that cache lives in the worker *process* (rule 12).

X changes its DOM without warning. When a capture breaks, check these four
before assuming the code rotted.

---

## 7. Screenshot quality gates exist for a reason

`shot_quality.py` rejects blank/black/half-loaded frames by pixel variance, and
the runner re-captures them. Video posts are the usual offender: shoot too early
and you get a black rectangle.

Do not remove the retry/quality passes to make runs faster. They are why the
output is trustworthy.

---

## 8. Change the input, not the builder

Two problems that looked like they needed frozen-code edits, solved from
outside:

* **`.xls` and `.tsv` were broken.** `input_loader` sends every path through
  `load_excel` (an `.xls` fails the zip check and gets read as text), and parses
  `.tsv` with csv's *comma* dialect, gluing the account name onto the URL. Fix:
  the web layer normalises every upload to a canonical `.xlsx` first, then hands
  its grid to the frozen `input_loader._rows_from_grid` so the layout logic is
  not reimplemented.
* **"Tweet Links" printed above every screenshot.** The builder prints a row's
  category, and the builder is frozen. Fix: drop the Category column from the
  canonical sheet. `input_loader` then defaults every row to `"Uncategorized"`,
  and `_has_categories()` returns `False` — which is the exact condition the
  builder uses.

**Pattern:** when a frozen component behaves wrongly, look at what you feed it.

---

## 9. Hosting: this app wants a real server

It now runs on an ordinary always-on VPS with root, which removes every
workaround below. **Requirements: Docker, ≥4 GB RAM, a persistent disk.** Shared
or "web" hosting cannot run Chromium.

The free-tier detour is worth remembering only so nobody repeats it:

| Host | Verdict |
|---|---|
| **Own VPS (Hostinger KVM, or similar)** | what this uses. No caps, real disk, real background workers |
| Oracle Cloud Always Free | free forever, always-on — card needed, ARM capacity often unavailable |
| Google Cloud Run | scale-to-zero — card needed, 60-min request ceiling |
| Render free + Browserless free | no card, but 512 MB, sleeps, no disk, and a browser-minute cap |
| Hugging Face Spaces | **Docker requires a paid plan** since July 2026 |
| Vercel / serverless functions | cannot run a minutes-long backend |
| Railway | trial credits only |

Free hosts forced two ugly adaptations, both since reverted: an external cookie
store (no disk) and running the capture inside the HTTP request (the CPU stops
once a response is sent). If you ever go back to such a host, those are the two
things you will have to rebuild — `EXECUTION_MODE=inline` still exists for the
second.

---

## 10. Memory: measure, do not guess

Measured with the browser *off-box*, so this is the app alone:

| Report | App memory |
|---|---|
| 5 links | 366 MB |
| 20 links | 413 MB |

On your own server add roughly **0.5–1 GB per Chromium worker** on top. That is
where `WORKERS` comes from: one per 1–1.5 GB of free RAM.

Re-measure before raising `WORKERS` or `MAX_LINKS`:

```bash
docker stats --no-stream --format '{{.MemUsage}}' <container>
```

An out-of-memory kill looks like a job dying with no error and the container
restarting — not like an exception.

---

## 11. Do not poll for status on an auto-scaling host

> Not a concern on a single server — one instance, one database. It matters the
> moment you run more than one replica.

Every instance has its own SQLite. A status poll can land on an instance that
has never heard of the job and answer `404` while the capture runs perfectly
somewhere else.

`/run-inline` therefore streams **NDJSON — one full job status per line**, the
same shape as the status endpoint, and the page renders from that stream. The
streaming response is pinned to the instance doing the work, so it is the only
status source that is always right.

---

## 12. Concurrency is bounded by RAM

Total simultaneous browsers = `MAX_CONCURRENT_JOBS × WORKERS`, and each browser
is ~0.5–1 GB. Overshoot and the kernel kills a job mid-run.

* The **Influencer report uses one worker** (`INFLUENCER_WORKERS=1`). Its
  follower-count cache lives in the worker *process*, so a second worker
  re-fetches the same profiles. Raise it only if your lists rarely repeat an
  account.
* `shm_size: "1gb"` in `docker-compose.yml` is not optional. Docker's default
  64 MB of shared memory makes Chromium crash on media-heavy posts.

---

## 13. Secrets

* `.env` and `sessions/x_state.json` are gitignored, absent from the image, and
  never served. Check `git ls-files` after any restructure.
* Use a **dedicated, throwaway X account**. Bulk captures get accounts
  rate-limited and suspended.
* `sessions/x_state.json` is a live login — whoever holds it can act as the
  capture account. Keep it on the server only; never commit it, never put it in
  a repo.
* **Never delete a hand-uploaded cookie.** `invalidate()` only removes the
  session when credentials exist to recreate it; otherwise the admin is left
  with nothing and no way back.
* `printf`, not `echo`, when piping a secret — a trailing newline becomes part
  of the password and the login fails in a way that looks like a wrong password.

---

## 14. Document-building gotchas

**DOCX**

* Nested tables are unreliable: under a fixed layout Word takes column widths
  from `w:tblGrid`, which `python-docx` does **not** update when you set
  `cell.width`. Prefer **paragraph borders + a right tab stop** — every renderer
  lays those out the same way.
* A right tab stop must sit **inside** the cell's text area (cell width less
  Word's 0.08 in side margins) or it is silently discarded and the value lands
  mid-line.
* `cell.add_table()` appends an empty paragraph after the table. Reuse it
  instead of adding your own, or you get stray blank lines.

**PDF (reportlab)**

* Use `VALIGN BOTTOM`, not `MIDDLE`, when a row mixes font sizes — middle
  alignment leaves the larger text visibly sitting below its label.
* Register a Unicode TTF or non-Latin titles render as black boxes.

**Both**

* macOS Quick Look is not a renderer. It ignores nested-table widths and
  substitutes fonts. Verify a DOCX in Word/WPS, or by inspecting the XML.

---

## 15. Front-end gotcha that will waste an hour

An author `display` rule beats the `hidden` attribute. Without this, everything
you mark `hidden` still renders:

```css
[hidden] { display: none !important; }
```

---

## 16. Playwright habits

* `locator(sel).first` can resolve to a **hidden** element and time out while a
  visible one sits right there. Use `.locator("visible=true").first`.
* Comma-separated selectors resolve as one set — if the alternatives differ in
  visibility, try them one at a time.
* Element screenshots do not save you from a broken page layout: if something is
  painted *over* your element, it is in the pixels either way.

---

## 17. Third-party APIs are eventually consistent

GitHub's Contents API can return 404 for a file you just wrote. The store's
save→read round-trip failed once for exactly this reason and returned `False`
**silently**.

**Rule: log every failure branch.** A silent `return False` in a cold-start path
is indistinguishable from a misconfiguration, and you will debug the wrong thing.

---

## 18. When adding a third report type

The Influencer report is the template for how to do this without touching frozen
code:

1. New folder, parallel to `src/` — capture, runner, worker, builder, entrypoint.
2. Its worker imports its own capture **directly**, so the frozen dispatcher and
   `src/_worker.py` need no routing change.
3. It may import `shot_quality`, `input_loader`, `platforms` and
   `browser_backend` read-only.
4. Add it to `REPORT_TYPES` in `config.py` and to `build_command()` in
   `webapp/jobs/runner.py`.
5. Mirror the proven structure (retry pass, quality pass, `results.json`) rather
   than inventing a new one.

---

## Checklist before you ship a change

- [ ] `git status` on `run.py`, `src/`, `install.py`, `requirements.txt` is clean
- [ ] Ran **both** report types end to end
- [ ] **Opened the PDF and the DOCX and looked at them**
- [ ] Tested with a reply URL and a video post
- [ ] Checked peak memory if anything touches document building
- [ ] Confirmed `.env` / `sessions/` are still untracked
- [ ] Tested on the server, not only locally
