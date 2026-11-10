"""
Handle content topics that are an embedded player instead of a downloadable file

Two things happen to such a topic:
  * Its saved .html is rewritten to a small self-contained page that carries a
    <base href> plus links to the Brightspace page and the player, so it opens in a browser
  * A <name>.url shortcut is written next to it pointing at the Brightspace topic page, which always plays.

If `include.videos: true` the actual media file is downloaded next to the .html.
"""

from __future__ import annotations

import html as _html
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .limits import DownloadSkipped
from .util import log

_MARKER = "brightspace_sync:embed"
_IFRAME_RE = re.compile(r"<iframe\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MARKER_RE = re.compile(r"<!--\s*" + re.escape(_MARKER) + r"\s*(\{.*?\})\s*-->", re.S)


@dataclass
class MediaInfo:
    provider: str  # kaltura | youtube | vimeo | panopto | iframe
    embed_url: str  # absolute URL of the iframe
    entry_id: str | None = None
    partner_id: str | None = None

    def as_marker(self) -> str:
        payload = {"provider": self.provider, "embed_url": self.embed_url,
                   "entry_id": self.entry_id, "partner_id": self.partner_id}
        return f"<!-- {_MARKER} {json.dumps(payload, separators=(',', ':'))} -->"

    def ytdlp_target(self) -> str:
        if self.provider == "kaltura" and self.partner_id and self.entry_id:
            return f"kaltura:{self.partner_id}:{self.entry_id}"
        if self.provider == "youtube" and self.entry_id:
            return f"https://www.youtube.com/watch?v={self.entry_id}"
        if self.provider == "vimeo" and self.entry_id:
            return f"https://vimeo.com/{self.entry_id}"
        return self.embed_url


# Detection / Extraction -----------------
def _classify(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "kaltura" in host:
        return "kaltura"
    if host.endswith(("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "youtube"
    if host.endswith("vimeo.com"):
        return "vimeo"
    if "panopto" in host:
        return "panopto"
    return "iframe"


def _kaltura_ids(url: str) -> tuple[str | None, str | None]:
    def find(*pats: str) -> str | None:
        for p in pats:
            m = re.search(p, url)
            if m:
                return m.group(1)
        return None
    partner = find(r"[?&]partner_id=(\d+)", r"/partner_id/(\d+)", r"/p/(\d+)/")
    entry = find(r"[?&]entry_id=([0-9A-Za-z_]+)", r"/entryId/([0-9A-Za-z_]+)",
                 r"[?&]entryId=([0-9A-Za-z_]+)")
    return partner, entry


def extract(markup: str) -> MediaInfo | None:
    """Return MediaInfo if `markup` is an embed wrapper, else None.

    Fast path: a marker comment left by a previous run.
    """
    m = _MARKER_RE.search(markup)
    if m:
        try:
            d = json.loads(m.group(1))
            return MediaInfo(provider=d.get("provider", "iframe"),
                             embed_url=d.get("embed_url", ""),
                             entry_id=d.get("entry_id"),
                             partner_id=d.get("partner_id"))
        except (ValueError, TypeError):
            pass

    iframes = _IFRAME_RE.findall(markup)
    if len(iframes) != 1:
        return None
    body = markup.split("<body", 1)[-1]
    text = _html.unescape(_TAG_RE.sub(" ", body))
    if len(re.sub(r"\s+", " ", text).strip()) > 200:
        return None  # a real HTML lesson that merely contains one iframe

    src = _html.unescape(iframes[0].strip())
    if src.startswith("//"):
        src = "https:" + src
    if not src.startswith("http"):
        return None

    provider = _classify(src)
    info = MediaInfo(provider=provider, embed_url=src)
    if provider == "kaltura":
        info.partner_id, info.entry_id = _kaltura_ids(src)
    elif provider == "youtube":
        m = re.search(r"/embed/([\w-]{11})", src) or re.search(r"[?&]v=([\w-]{11})", src)
        info.entry_id = m.group(1) if m else None
    elif provider == "vimeo":
        m = re.search(r"/video/(\d+)", src)
        info.entry_id = m.group(1) if m else None
    return info


# Rewrite ---------------
def rewrite_html(info: MediaInfo, *, base_url: str, stream_page: str, title: str) -> str:
    tag = (f'<iframe src="{_html.escape(info.embed_url)}" '
           f'allow="autoplay *; fullscreen *; encrypted-media *" '
           f'allowfullscreen frameborder="0" title="{_html.escape(title)}"></iframe>')
    t = _html.escape(title or "Video")
    return (
        f"{info.as_marker()}\n"
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{t}</title><base href=\"{_html.escape(base_url)}/\">"
        "<style>body{font:14px/1.6 system-ui,-apple-system,sans-serif;margin:2rem;"
        "max-width:900px}.bar{margin-bottom:1rem}.bar a{margin-right:1rem}"
        "iframe{width:100%;aspect-ratio:16/9;height:auto;border:0}"
        "small{color:#666}</style></head><body>"
        f"<div class=\"bar\"><strong>{t}</strong><br>"
        f"<a href=\"{_html.escape(stream_page)}\" target=\"_blank\">Open in Brightspace</a>"
        f"<a href=\"{_html.escape(info.embed_url)}\" target=\"_blank\">Open player directly</a>"
        "</div>"
        f"{tag}"
        f"<p><small>Saved by brightspace_sync. This is a {info.provider} embed - "
        "it plays here only while you're online and signed in to Brightspace. "
        "The <code>.url</code> file next to this one opens the Brightspace page "
        "directly.</small></p></body></html>"
    )


def write_pointer(dest: Path, stream_page: str) -> None:
    dest.write_text(f"[InternetShortcut]\nURL={stream_page}\n", "utf-8")


# Download -----------------------
@dataclass
class VideoOpts:
    """How to fetch videos. Built once per course in sync.py from the config."""
    enabled: bool = False
    rate_limit: str = ""          # "" = full speed; else "3M", "800K", ...
    max_height: int | None = 1080  # cap resolution; None = best available
    timeout: int = 7200            # seconds per video (binary path only); 0 = none
    archive: Path | None = None    # yt-dlp download-archive file (resume / dedup)
    budget: object = None

_RATE_RE = re.compile(r"^\s*([\d.]+)\s*([kmg]?)i?b?\s*$", re.I)

def _parse_rate(s: str) -> int | None:
    if not s:
        return None
    m = _RATE_RE.match(str(s))
    if not m:
        log.warning("video_rate_limit %r not understood - ignoring", s)
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    return int(n * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()])

def find_ytdlp() -> str | None:
    """
    An explicit yt-dlp *binary* to prefer over the bundled/importable module
    (lets a user drop a newer yt-dlp next to the executable or on PATH).
    """
    for base in {Path(sys.executable).parent, Path(sys.argv[0]).resolve().parent}:
        for name in ("yt-dlp", "yt-dlp.exe"):
            if (base / name).is_file():
                return str(base / name)
    return shutil.which("yt-dlp")


def download_video(
    info: MediaInfo,
    *,
    dest_dir: Path,
    base_name: str,
    base_url: str,
    opts: VideoOpts,
    progress=None,
) -> tuple[str, Path | None]:
    """
    Returns (status, path). status: 'ok' | 'exists' | 'no-ytdlp' | 'error: ...'.

    Uses an explicit yt-dlp binary if one is found, otherwise the bundled
    `yt_dlp` Python module (a hard dependency, so this normally always works).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    had_file = _existing_video(dest_dir, base_name) is not None
    target = info.ytdlp_target()
    out_tmpl = str(dest_dir / (base_name + ".%(ext)s"))
    archive = str(opts.archive or (dest_dir / ".video-archive.txt"))

    binary = find_ytdlp()
    before = set(dest_dir.iterdir())
    try:
        if binary and opts.budget is None:
            rc, err = _via_binary(binary, target, out_tmpl, base_url, archive, opts)
        else:
            rc, err = _via_api(target, out_tmpl, base_url, archive, opts, progress)
            if rc is None:
                return "no-ytdlp", None
    except BaseException as exc:
        for path in dest_dir.iterdir():
            if (path not in before and path.is_file() and path.name.startswith(base_name + '.')
                    and ('.part' in path.name or path.suffix == '.ytdl')):
                path.unlink(missing_ok=True)
        if isinstance(exc, DownloadSkipped):
            log.info("  %s", exc)
            return "declined", None
        raise

    got = _existing_video(dest_dir, base_name)
    if rc == 0:
        return ("exists" if had_file else "ok"), got
    return f"error: {err}", got


def _fmt(max_height: int | None) -> str | None:
    if not max_height:
        return None
    return (f"bv*[height<={max_height}]+ba/b[height<={max_height}]"
            f"/bv*+ba/b")


def _via_api(target, out_tmpl, base_url, archive, opts: VideoOpts, progress):
    try:
        import yt_dlp
    except Exception:  # noqa: BLE001 - not installed / bundle stripped
        return None, None
    
    y = {
        "outtmpl": out_tmpl,
        "noplaylist": True,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "http_headers": {"Referer": base_url.rstrip("/") + "/"},
        "download_archive": archive,
        "retries": 3, "fragment_retries": 3, "socket_timeout": 30,
    }

    rl = _parse_rate(opts.rate_limit)
    if rl:
        y["ratelimit"] = rl

    fmt = _fmt(opts.max_height)
    if fmt:
        y["format"] = fmt
    from .limits import progress_hook, youtube_dl_class, postprocess_hook
    y["progress_hooks"] = [progress_hook(opts.budget)] + ([progress] if progress else [])
    y['postprocessor_hooks'] = [postprocess_hook(opts.budget)]
    if opts.budget is not None:
        # Keep transfers serial so one shared budget can pause reliably.
        y['concurrent_fragment_downloads'] = 1
        y['hls_prefer_native'] = True
        y['external_downloader'] = 'native'

    try:
        with youtube_dl_class(yt_dlp, opts.budget)(y) as ydl:
            return ydl.download([target]), None
    except Exception as exc:  # noqa: BLE001 - DownloadError etc.
        last = (str(exc).strip().splitlines() or ["download failed"])[-1]
        return 1, last


def _via_binary(ytdlp, target, out_tmpl, base_url, archive, opts: VideoOpts):
    cmd = [
        ytdlp, "--no-playlist", "--no-progress", "--no-warnings",
        "--referer", base_url.rstrip("/") + "/",
        "--retries", "3", "--fragment-retries", "3", "--socket-timeout", "30",
        "--download-archive", archive, "-o", out_tmpl,
    ]
    if opts.rate_limit:
        cmd += ["--limit-rate", opts.rate_limit]
    if opts.max_height:
        cmd += ["-S", f"res:{opts.max_height}"]
    cmd.append(target)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=opts.timeout or None)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)
    if r.returncode == 0:
        return 0, None
    msg = [ln for ln in (r.stderr or r.stdout or "").strip().splitlines() if ln.strip()]
    return r.returncode, (msg[-1] if msg else f"yt-dlp exit {r.returncode}")

_VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".mp3", ".m4a")

def _existing_video(dest_dir: Path, base_name: str) -> Path | None:
    for ext in _VIDEO_EXT:
        p = dest_dir / (base_name + ext)
        if p.is_file():
            return p
    return None
