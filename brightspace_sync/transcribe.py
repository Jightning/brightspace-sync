"""
Turn an embedded lecture video into a readable `<name> - transcript.md`.

Two sources are tried, cheapest first:

  1. Publisher captions. Kaltura / YouTube / Panopto usually already serve a
     caption track for the entry. yt-dlp can fetch it on its own, without
     touching the video, so this costs one small request and is as accurate as
     whoever wrote the captions.
  2. Local speech recognition, for entries with no caption track. Needs the
     optional extra:  pip install "brightspace-sync[transcribe]"
     which pulls in faster-whisper. The audio comes from the video file if
     `include.videos` already saved one, otherwise an audio-only copy is
     fetched to a temp folder and deleted afterwards.

Output is one Markdown file per video: a short header saying where the text
came from, then timestamped paragraphs.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .media import MediaInfo
from .util import log

SUFFIX = " - transcript.md"

# A cue is (start seconds, end seconds, one line of text).
Cue = "tuple[float, float, str]"

_PARAGRAPH_CHARS = 600          # start a new timestamped paragraph past this
_PARAGRAPH_GAP = 2.0            # ...or after this much actual silence, in seconds
_SUB_LANGS = ["en.*", "en"]     # preference order handed to yt-dlp


@dataclass
class TranscribeOpts:
    """How to transcribe. Built once per course in sync.py, like VideoOpts."""
    enabled: bool = False
    model: str = "base"           # faster-whisper size: tiny|base|small|medium|large-v3
    language: str | None = None   # None = let the model detect it


# Public entry point -----------------------------------------------------
def transcribe(
    info: MediaInfo,
    *,
    dest_dir: Path,
    base_name: str,
    title: str,
    base_url: str,
    video_path: Path | None,
    opts: TranscribeOpts,
    force: bool = False,
) -> tuple[str, Path | None]:
    """Write `<base_name> - transcript.md` next to the topic.

    Returns (status, path). status is one of:
    'captions' | 'asr' | 'exists' | 'no-captions' | 'no-whisper' | 'error: ...'
    """
    dest = dest_dir / (base_name + SUFFIX)
    if dest.is_file() and not force:
        return "exists", dest

    try:
        cues, lang = _fetch_captions(info, base_url)
    except Exception as exc:  # noqa: BLE001 - a bad caption file is not fatal
        log.debug("caption fetch failed for %s: %s", title, exc)
        cues, lang = [], None

    source = "publisher captions"
    if not cues:
        status, cues, lang = _speech_to_text(info, base_url, video_path, opts)
        if status:
            return status, None
        source = f"speech recognition (faster-whisper {opts.model})"

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(_render(title, cues, source, lang), "utf-8")
    return ("captions" if source == "publisher captions" else "asr"), dest


# 1. Publisher captions --------------------------------------------------
def _fetch_captions(info: MediaInfo, base_url: str) -> tuple[list, str | None]:
    """Ask yt-dlp for the entry's caption track (no video download)."""
    try:
        import yt_dlp
    except ImportError:
        return [], None

    with tempfile.TemporaryDirectory(prefix="bs-subs-") as tmp:
        tmpdir = Path(tmp)
        y = {
            "skip_download": True,
            "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": _SUB_LANGS, "subtitlesformat": "srt/vtt/best",
            "outtmpl": str(tmpdir / "cap.%(ext)s"),
            "http_headers": {"Referer": base_url.rstrip("/") + "/"},
            "quiet": True, "no_warnings": True, "noprogress": True,
            "retries": 2, "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(y) as ydl:  # type: ignore[attr-defined]
            ydl.download([info.ytdlp_target()])

        # yt-dlp names the file cap.<lang>.<FORMAT>, and the format casing
        # follows whatever the provider called it ('SRT' on Kaltura).
        files = [p for p in tmpdir.iterdir()
                 if p.suffix.lower() in (".srt", ".vtt") and p.stat().st_size]
        if not files:
            return [], None
        picked = files[0]
        lang = picked.name.split(".")[1] if picked.name.count(".") >= 2 else None
        return _parse_cues(picked.read_text("utf-8", errors="replace")), lang


_STAMP_RE = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_TIME_RE = re.compile(_STAMP_RE + r"\s*-->\s*" + _STAMP_RE)


def _seconds(h, mm, ss, ms) -> float:
    return int(h or 0) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")) / 1000


def _parse_cues(text: str) -> list:
    """Parse SRT or WebVTT into [(start, end, line)] in seconds. Both formats
    are a '-->' timing line followed by the lines it applies to."""
    cues: list = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = _TIME_RE.search(lines[i])
        i += 1
        if not m:
            continue
        g = m.groups()
        start, end = _seconds(*g[:4]), _seconds(*g[4:])
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            body.append(_strip_markup(lines[i]))
            i += 1
        joined = " ".join(b for b in body if b).strip()
        # Rolling captions repeat the previous line; keep only what is new.
        if joined and (not cues or joined != cues[-1][2]):
            cues.append((start, end, joined))
    return cues


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_markup(line: str) -> str:
    """Caption lines can carry <c>/<i> styling and karaoke timestamps."""
    line = re.sub(r"<\d{2}:\d{2}:\d{2}[.,]\d{1,3}>", "", line)
    return re.sub(r"\s+", " ", _TAG_RE.sub("", line)).strip()


# 2. Local speech recognition -------------------------------------------
def _speech_to_text(info: MediaInfo, base_url: str, video_path: Path | None,
                    opts: TranscribeOpts) -> tuple[str, list, str | None]:
    """Returns (error status, cues, language). An empty status means success."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return "no-whisper", [], None

    if video_path and video_path.is_file():
        return _run_whisper(WhisperModel, video_path, opts)

    with tempfile.TemporaryDirectory(prefix="bs-audio-") as tmp:
        audio = _download_audio(info, base_url, Path(tmp))
        if not audio:
            return "no-captions", [], None
        return _run_whisper(WhisperModel, audio, opts)


def _download_audio(info: MediaInfo, base_url: str, tmpdir: Path) -> Path | None:
    try:
        import yt_dlp
    except ImportError:
        return None
    y = {
        "format": "ba/b", "noplaylist": True,
        "outtmpl": str(tmpdir / "audio.%(ext)s"),
        "http_headers": {"Referer": base_url.rstrip("/") + "/"},
        "quiet": True, "no_warnings": True, "noprogress": True,
        "retries": 3, "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(y) as ydl:  # type: ignore[attr-defined]
            ydl.download([info.ytdlp_target()])
    except Exception as exc:  # noqa: BLE001 - caller reports it as 'no audio'
        log.debug("audio-only download failed: %s", exc)
        return None
    files = [p for p in tmpdir.iterdir() if p.is_file() and p.stat().st_size]
    return files[0] if files else None


def _run_whisper(WhisperModel, audio: Path, opts: TranscribeOpts) -> tuple[str, list, str | None]:
    try:
        model = WhisperModel(opts.model, device="auto", compute_type="int8")
        segments, meta = model.transcribe(str(audio), language=opts.language,
                                          vad_filter=True)
        cues = [(seg.start, seg.end, seg.text.strip())
                for seg in segments if seg.text.strip()]
    except Exception as exc:  # noqa: BLE001 - model download / decode failures
        return f"error: {str(exc).strip().splitlines()[-1]}", [], None
    if not cues:
        return "no-captions", [], None
    return "", cues, getattr(meta, "language", None)


# Rendering ---------------------------------------------------------------
def _stamp(seconds: float) -> str:
    s = int(seconds)
    return (f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600
            else f"{s // 60}:{s % 60:02d}")


def _paragraphs(cues: list) -> list:
    """Group cues into readable blocks, each keyed by the time it starts. A new
    block begins after a pause in the speech, or once one gets long enough to
    be hard to read."""
    out: list = []
    start, buf, prev_end = None, [], None
    for t, end, line in cues:
        if start is None:
            start = t
        elif (t - prev_end > _PARAGRAPH_GAP
              or len(" ".join(buf)) > _PARAGRAPH_CHARS):
            out.append((start, " ".join(buf)))
            start, buf = t, []
        buf.append(line)
        prev_end = end
    if buf and start is not None:
        out.append((start, " ".join(buf)))
    return out


def _render(title: str, cues: list, source: str, lang: str | None) -> str:
    head = f"# {title}\n\n_Transcript - {source}"
    head += f", language: {lang}_\n\n" if lang else "_\n\n"
    body = "\n\n".join(f"**[{_stamp(t)}]** {text}" for t, text in _paragraphs(cues))
    return head + body + "\n"
