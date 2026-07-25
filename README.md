---
title: Report Automation
emoji: 📄
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Build X/Twitter and Influencer reports from a list of links
---

<!-- The YAML block above configures the Hugging Face Space (see DEPLOY.md).
     It is ignored by GitHub and by every command below — the CLI is unchanged. -->

# X (Twitter) Report Automation

Give it a list of **X / Twitter post links** in an Excel sheet. For every link it
opens the post in your logged-in Chrome, captures **one clean screenshot of the
tweet** (header → text → media — engagement bar and your own account UI cropped
out), verifies each shot actually rendered (recapturing any that come out
black / blank / half-loaded), and builds a **PDF and a Word (.docx)** report,
saved to `reports/`.

Runs on macOS, Windows and Linux.

> Rebuilding the whole thing from scratch? Everything you need is in
> **[BLUEPRINT.md](BLUEPRINT.md)**.

---

## Setup (once)

```bash
python install.py          # installs dependencies + the browser
python save_login.py x     # a Chrome window opens — log into X by hand
```

Log in with your **own username + password** (avoid "Continue with Google"). The
session saves itself the moment you're in. Re-run `python save_login.py x`
whenever X starts asking you to log in again (sessions expire).

---

## Everyday use

Put your links in `config/links.xlsx` (a column of X/Twitter post URLs), then:

```bash
python run.py config/links.xlsx
```

That screenshots every link, runs the quality/verification pass, and writes
`reports/Twitter_Report_<date>.pdf` and `.docx`.

Common options:

```bash
python run.py config/links.xlsx --workers 8          # faster for long lists
python run.py config/links.xlsx --date 25-07-26      # date in the header + filename (dd-mm-yy)
python run.py config/links.xlsx --title "My Report"  # header label (default: "Twitter Report")
python run.py config/links.xlsx --headed             # watch the browser work
python run.py -                                      # paste links on stdin, then Ctrl-D
```

**The report:** a dated header, then **one tweet per page** (screenshot centered
at the top, link left-aligned beneath), then a **links list** at the end. Broken
links are skipped; screenshots are compressed so the files stay small.

---

## The input sheet

- A column of X/Twitter post URLs. If a header row names the columns
  (`link` / `url` / `post link`, and optionally `account` / `handle` and
  `category`), those are used; otherwise any cell with a URL is treated as a link.
- A non-URL line/row becomes a **section header** (category) for the links under
  it (optional).
- Only X / Twitter links are captured; anything else is skipped with a note.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Links come back `login_wall` | Re-run `python save_login.py x` |
| Some links `not_found` under load | The retry + quality passes re-attempt them; lower `--workers` if it persists |
| A screenshot looks off | X may have changed its DOM — see the capture/crop notes in `src/capture/x_capture.py` (and [BLUEPRINT.md](BLUEPRINT.md) §6) |
| `python` runs Python 2 | Use `python3` for every command |

**Security:** `sessions/` holds your live X login cookies — never commit or share
it. It's already gitignored.
```
