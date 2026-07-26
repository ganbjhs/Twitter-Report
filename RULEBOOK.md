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

There is exactly one edit in the whole of `src/`, and it was explicitly
approved: two lines in `src/_worker.py` swapping `p.chromium.launch(...)` for
`launch_browser(p, headless)`.

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

## 9. Free hosts: what actually works

Checked in 2026; re-check before trusting any of it.

| Host | Verdict |
|---|---|
| **Render free + Browserless free** | works, no card. ~512 MB RAM, sleeps after 15 min |
| Oracle Cloud Always Free | works, always-on, real disk — needs a card |
| Google Cloud Run | works, scale-to-zero — needs a card |
| Hugging Face Spaces | **Docker requires a paid plan** since July 2026 |
| Vercel / serverless functions | cannot run a minutes-long backend |
| Railway | trial credits only |

Two structural constraints follow from the free tier:

* **No disk.** The X session must live somewhere external
  (`webapp/x_state_store.py`) or every cold start re-signs-in and burns metered
  browser minutes.
* **The CPU stops when the response is sent.** A background worker freezes
  mid-capture. That is what `EXECUTION_MODE=inline` is for: the capture runs
  *inside* the request and streams progress back.

---

## 10. Memory is the binding constraint, and it is measured

| Report | App memory |
|---|---|
| 5 links | 366 MB |
| 20 links | 413 MB |
| Render free limit | **512 MB** |

`MAX_LINKS=25` is not a guess. Do not raise it without re-measuring:

```bash
docker stats --no-stream --format '{{.MemUsage}}' <container>
```

An out-of-memory kill on Render looks like a job dying with no error and the
service restarting — not like an exception.

---

## 11. Do not poll for status on an auto-scaling host

Every instance has its own SQLite. A status poll can land on an instance that
has never heard of the job and answer `404` while the capture runs perfectly
somewhere else.

`/run-inline` therefore streams **NDJSON — one full job status per line**, the
same shape as the status endpoint, and the page renders from that stream. The
streaming response is pinned to the instance doing the work, so it is the only
status source that is always right.

---

## 12. Concurrency is capped by the browser plan, not by your CPU

Total simultaneous browsers = `MAX_CONCURRENT_JOBS × CAPTURE_WORKERS`. A free
remote-browser plan allows **2**. Exceeding it does not queue — it errors, as
`Target page, context or browser has been closed` partway through a run.

`config.py` clamps this automatically. Two related points:

* The **Influencer report uses one worker** (`INFLUENCER_WORKERS=1`). Its
  follower-count cache is per worker *process*, so a second worker re-fetches
  the same profiles — paying twice for the same data. One browser also leaves
  headroom under the 2-concurrent cap rather than sitting exactly on it.
* Raising workers on a metered browser costs money before it saves time.

---

## 13. Secrets

* `.env` and `sessions/x_state.json` are gitignored, absent from the image, and
  never served. Check `git ls-files` after any restructure.
* Use a **dedicated, throwaway X account**. Bulk captures get accounts
  rate-limited and suspended.
* The session store repo must be **private** with a repo-scoped token — whoever
  holds `x_state.json` can act as the capture account.
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
- [ ] Tested against the real host, not only locally — local Chromium hides
      viewport and codec problems
