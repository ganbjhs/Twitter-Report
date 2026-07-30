"""Upload handling: parse → validate → normalize to a canonical .xlsx.

Why normalize instead of handing the raw upload to the pipeline:

  * `src/input_loader.py` routes every path through `load_excel`, which needs a
    real .xlsx. A legacy binary `.xls` fails the zip check and is then read as
    text — producing garbage. A `.tsv` is parsed with csv's *comma* dialect, so
    "Name<TAB>URL" collapses into one cell and the link comes out as
    "Name\\thttps://…". Normalizing here fixes both **without editing the frozen
    loader** (rule #1).
  * It lets us reject a bad file with a useful message *before* a job is queued,
    and show the user how many links were found.

The layout logic itself (header row vs plain list, running category headers, URL
cleanup, X-only filtering) is NOT reimplemented — we build a grid and hand it to
the frozen `input_loader._rows_from_grid`, so the web app and the CLI agree on
exactly what a given sheet means.
"""
import csv
import io
import re
import sys
from pathlib import Path

from . import config

# The frozen loader is imported read-only — never modified.
sys.path.insert(0, str(config.ROOT / "src"))
import input_loader  # noqa: E402

ALLOWED_SUFFIXES = (".xlsx", ".xls", ".csv", ".tsv", ".txt")

_URL_RE = re.compile(r"https?://\S+", re.I)


class UploadError(Exception):
    """A user-facing problem with the uploaded file."""


# --------------------------------------------------------------------------- #
# Name sanitizing
# --------------------------------------------------------------------------- #
def safe_stem(name: str, fallback: str = "Report") -> str:
    """Turn the user's 'Report / File Name' into a filesystem-safe stem.

    Same spirit as run.py's `re.sub(r"[^0-9A-Za-z._-]+", "_", …)`, plus explicit
    traversal defence: no separators, no leading dots, length capped.
    """
    name = (name or "").strip()
    name = name.replace("/", " ").replace("\\", " ")
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("._-")
    stem = re.sub(r"_{2,}", "_", stem)[:60]
    return stem or fallback


def display_title(name: str, fallback: str = "Report") -> str:
    """The human title shown in the document header — punctuation kept, control
    characters and newlines removed."""
    title = re.sub(r"[\x00-\x1f\x7f]", " ", (name or "")).strip()
    title = re.sub(r"\s{2,}", " ", title)[:80]
    return title or fallback


# Characters no mainstream filesystem accepts in a name. Everything else the
# user typed — spaces, dashes, brackets, non-Latin script — is kept, because the
# download is meant to read exactly like the header of the document inside it.
_ILLEGAL_IN_FILENAME = str.maketrans({c: " " for c in '/\\:*?"<>|'})


def download_name(title: str, fallback: str = "Report") -> str:
    """The name the browser saves the file under: the user's own text.

    This is a Content-Disposition value, never a path — the file on disk keeps
    the conservative `safe_stem` name — so only characters that would break a
    save dialog are replaced. A reserved Windows device name is suffixed rather
    than mangled, since "CON.pdf" cannot be written on Windows at all.
    """
    name = display_title(title, fallback).translate(_ILLEGAL_IN_FILENAME)
    name = re.sub(r"\s{2,}", " ", name).strip(" .")
    if not name:
        return fallback
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or \
            re.fullmatch(r"(?:COM|LPT)[1-9]", name.upper()):
        name += "_"
    return name


def safe_upload_name(filename: str) -> str:
    """A display-only version of the uploaded filename. Never used as a path."""
    base = Path((filename or "").replace("\\", "/")).name
    return re.sub(r"[\x00-\x1f\x7f]", "", base)[:120] or "upload"


# --------------------------------------------------------------------------- #
# Format readers -> grid (list of rows of strings)
# --------------------------------------------------------------------------- #
def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _grid_from_xlsx(path: Path) -> list:
    try:
        from openpyxl import load_workbook
    except ImportError as e:                                  # pragma: no cover
        raise UploadError("Server is missing openpyxl — cannot read .xlsx.") from e
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise UploadError(
            "That .xlsx could not be opened as an Excel workbook. If it is "
            "really a CSV or text file, rename it to .csv or .txt and re-upload."
        ) from e
    ws = wb.active
    grid = [[("" if c is None else str(c)).strip() for c in row]
            for row in ws.iter_rows(values_only=True)]
    wb.close()
    return grid


def _grid_from_xls(path: Path) -> list:
    try:
        import xlrd
    except ImportError as e:
        raise UploadError(
            "Server is missing xlrd — cannot read legacy .xls files. "
            "Re-save the file as .xlsx or .csv and upload again.") from e
    try:
        book = xlrd.open_workbook(str(path))
    except Exception as e:
        raise UploadError(
            "That .xls could not be opened. Re-save it as .xlsx or .csv."
        ) from e
    sheet = book.sheet_by_index(0)
    grid = []
    for r in range(sheet.nrows):
        row = []
        for c in range(sheet.ncols):
            v = sheet.cell_value(r, c)
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            row.append(str(v).strip() if v is not None else "")
        grid.append(row)
    return grid


def _sniff_delimiter(text: str, default: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except Exception:
        # Fall back on whichever candidate actually appears most often.
        counts = {d: sample.count(d) for d in (",", "\t", ";", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else default


def _grid_from_text(path: Path, default_delim: str) -> list:
    text = _decode(path.read_bytes())
    if not text.strip():
        raise UploadError("That file is empty.")
    delim = _sniff_delimiter(text, default_delim)
    if not isinstance(delim, str) or len(delim) != 1:
        delim = ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except csv.Error:
        rows = [[line] for line in text.splitlines()]
    return [[(c or "").strip() for c in row] for row in rows]


def read_grid(path: Path, suffix: str) -> list:
    suffix = (suffix or path.suffix).lower()
    if suffix == ".xlsx":
        return _grid_from_xlsx(path)
    if suffix == ".xls":
        return _grid_from_xls(path)
    if suffix == ".tsv":
        return _grid_from_text(path, "\t")
    if suffix in (".csv", ".txt"):
        # A one-link-per-line .txt parses cleanly as single-column CSV, which is
        # exactly how the frozen loader treats a pasted list.
        return _grid_from_text(path, ",")
    raise UploadError(
        f"Unsupported file type '{suffix}'. Accepted: "
        + ", ".join(ALLOWED_SUFFIXES) + ".")


# --------------------------------------------------------------------------- #
# Parse + validate + write the canonical workbook
# --------------------------------------------------------------------------- #
def parse_rows(path: Path, suffix: str) -> list:
    """Uploaded file -> the pipeline's row contract, using the frozen loader's
    own layout logic. Raises UploadError with a message fit for the UI."""
    grid = read_grid(path, suffix)
    if not any(any(c for c in row) for row in grid):
        raise UploadError("That file has no content.")

    total_urls = sum(1 for row in grid for c in row if _URL_RE.search(c or ""))
    rows = input_loader._rows_from_grid(grid)   # read-only reuse — never edited

    if not rows:
        if total_urls:
            raise UploadError(
                f"Found {total_urls} link(s), but none of them are X/Twitter "
                "post links. This tool only captures x.com / twitter.com posts.")
        raise UploadError(
            "No links found. Put one X/Twitter post URL per row (or per line), "
            "or use a sheet with a column headed 'Link'.")
    if len(rows) > config.MAX_LINKS:
        raise UploadError(
            f"That file has {len(rows)} X links — the limit is "
            f"{config.MAX_LINKS} per job. Split it into smaller files.")
    return rows


def write_canonical_xlsx(rows: list, dest: Path) -> None:
    """Write rows as an Account | Link sheet.

    Those exact headers are recognised by `input_loader._header_index`, so the
    pipeline reads back precisely the rows we validated, in the same order.

    NOTE — the category column is deliberately omitted. A sheet's category is
    almost always a section word like "Tweet Links", and the Twitter report
    prints it above every screenshot, which is just noise in the finished
    document. `input_loader` defaults a missing category to "Uncategorized", and
    the report builder only prints the label when some row has a real category —
    so leaving the column out removes it from the document without touching the
    frozen builder. (The Influencer report shows the account name instead.)
    """
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Links"
    ws.append(["Account", "Link"])
    for r in rows:
        ws.append([r.get("account_name", ""), r.get("link", "")])
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def suffix_of(filename: str) -> str:
    suffix = Path((filename or "").replace("\\", "/")).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise UploadError(
            f"'{safe_upload_name(filename)}' is not a supported file type. "
            "Accepted: " + ", ".join(ALLOWED_SUFFIXES) + ".")
    return suffix
