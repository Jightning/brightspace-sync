from __future__ import annotations

import hashlib
import html as _html
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("brightspace_sync")

_CONSOLE_FMT = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")


def setup_logging(verbose: bool = False) -> None:
    """Console logging. The file log (added later in cli._prepare) always records
    DEBUG; the console shows INFO unless -v/--verbose."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # let handlers decide what to show/keep
    if not any(getattr(h, "_bs_console", False) for h in root.handlers):
        con = logging.StreamHandler()
        con._bs_console = True  # type: ignore[attr-defined]
        con.setFormatter(_CONSOLE_FMT)
        root.addHandler(con)
    for h in root.handlers:
        if getattr(h, "_bs_console", False):
            h.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")

_TABLE_RE = re.compile(r"(?is)<table\b.*?</table\s*>")
_ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>")
_CELL_RE = re.compile(r"(?is)<(t[dh])\b([^>]*)>(.*?)</\1\s*>")
_SPAN_RE = re.compile(r"""(?i)\b(row|col)span\s*=\s*["']?(\d+)""")
_MAX_SPAN = 50   # a malformed span must not blow the row up


def _cell(markup: str) -> str:
    """One table cell as a single line. Entities are left alone - the caller
    unescapes the whole document once, at the end."""
    t = re.sub(r"(?i)<br\s*/?>", " ", markup)
    t = _TAG_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip().replace("|", r"\|")


def _spans(attrs: str) -> tuple[int, int]:
    """(colspan, rowspan) off a <td>/<th>, defaulting to 1."""
    found = {k.lower(): int(v) for k, v in _SPAN_RE.findall(attrs)}
    return (max(1, min(_MAX_SPAN, found.get("col", 1))),
            max(1, min(_MAX_SPAN, found.get("row", 1))))


def _md_table(markup: str) -> str:
    """An HTML <table> as a Markdown table. Markdown has no merged cells, so a
    cell that spans rows or columns is repeated into every slot it covers -
    that keeps a schedule whose "Week" column spans several rows lined up under
    the right headings. A table nested inside another is flattened (the outer
    match stops at the first </table>) - rare enough in course pages to not be
    worth a real parser."""
    rows: list[list[str]] = []
    carry: dict[int, list] = {}   # column -> [text, rows of it still to fill]

    def take(row: list[str], col: int) -> int:
        """Fill slots this column still owes from a rowspan further up."""
        while col in carry:
            row.append(carry[col][0])
            carry[col][1] -= 1
            if not carry[col][1]:
                del carry[col]
            col += 1
        return col

    for r in _ROW_RE.finditer(markup):
        row: list[str] = []
        col = 0
        for c in _CELL_RE.finditer(r.group(1)):
            col = take(row, col)
            text = _cell(c.group(3))
            across, down = _spans(c.group(2))
            for _ in range(across):
                row.append(text)
                if down > 1:
                    carry[col] = [text, down - 1]
                col += 1
        take(row, col)
        if row:
            rows.append(row)

    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(head) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n\n" + "\n".join(lines) + "\n\n"


def html_to_text(markup: str) -> str:
    """Rough HTML -> readable plain text (keeps line breaks / list bullets,
    and renders tables as Markdown tables)."""
    if not markup:
        return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", markup)
    t = _TABLE_RE.sub(lambda m: _md_table(m.group(0)), t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p\s*>", "\n\n", t)
    t = re.sub(r"(?i)<li[^>]*>", "\n- ", t)
    t = re.sub(r"(?i)</(div|h[1-6]|tr|ul|ol)\s*>", "\n", t)
    t = _TAG_RE.sub("", t)
    t = _html.unescape(t)
    return _WS_RE.sub("\n\n", t).strip()

_slug_strip = re.compile(r"[^\w\s.\-()&]+", re.UNICODE)
_slug_ws = re.compile(r"[\s_]+")

# Ensures string can be used for path
def slugify(text: str, *, max_len: int = 120) -> str:
    text = unicodedata.normalize("NFKC", (text or "").strip())
    text = _slug_strip.sub(" ", text)
    text = _slug_ws.sub(" ", text).strip(" .-")
    text = text.replace("/", "-").replace("\\", "-")

    if not text:
        text = "untitled"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .-")

    # Windows device names, just in case the tree is copied there.
    if text.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        text = "_" + text

    return text

def sha256_file(path: Path, _bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()

    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_bufsize), b""):
            h.update(chunk)

    return h.hexdigest()

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# ISO string to datetime
def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def human_size(num: int | None) -> str:
    if not num:
        return "0 B"
    
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < step: # type: ignore
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"

        num /= step # type: ignore

    return f"{num:.1f} PB"
