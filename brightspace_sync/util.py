from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("brightspace_sync")

def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    logging.getLogger("urllib3").setLevel(logging.WARNING)

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
