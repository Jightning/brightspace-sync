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


def html_to_text(markup: str) -> str:
    """Rough HTML -> readable plain text (keeps line breaks / list bullets)."""
    if not markup:
        return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", markup)
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
