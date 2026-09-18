"""
Traverses a course's Content tree and mirrors it locally.

Modules contain submodules and topics; topics are files or links. Modules and
topics can also carry rich text (paragraphs / images shown above and below the
linked files) - that lives in the 'unstable' TOC endpoint's Description field,
which is what this module reads first.

Output layout under <course folder>/Content/:
    <Module>/<Submodule>/<Topic Title>.<ext>       # file topics
    <Module>/<Submodule>/<Topic Title>.url         # link topics (INI shortcut)
    <Module>/_module.md, _module.html              # a module's own rich text
    <Module>/<Topic Title> - notes.md/.html        # a topic's rich text
    <Module>/_assets/                               # images pulled from that text
Plus:
    content_index.json   machine-readable tree (paths, descriptions, metadata)
    content_index.md     human-readable outline with the text inline
"""

from __future__ import annotations

import html as _html
import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from . import media
from .client import Brightspace
from .download import download_to
from .enrollments import Course
from .manifest import Manifest
from .transcribe import TranscribeOpts, transcribe
from .util import html_to_text, log, now_iso, slugify

TOPIC_FILE = 1
TOPIC_LINK = 3

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)("[^>]*>)', re.I)
_ASSET_CAP = 40                 # max images pulled per description
_MAX_DESC_HTML = 1_500_000      # skip asset-pulling on huge html blobs

_MODULE_HINTS = ("Structure", "ModuleStartDate", "ModuleEndDate", "ModuleDueDate")

# Sync numbers
@dataclass
class Stats:
    downloaded: int = 0
    updated: int = 0
    skipped: int = 0
    links: int = 0
    errors: int = 0
    embeds: int = 0
    videos: int = 0
    video_errors: int = 0
    transcripts: int = 0
    transcript_errors: int = 0
    descriptions: int = 0   # module/topic rich-text blocks saved
    assets: int = 0         # images pulled out of that rich text
    error_detail: list[str] = field(default_factory=list)

    def add(self, action: str, detail: str = "") -> None:
        if action == "downloaded":
            self.downloaded += 1
        elif action == "updated":
            self.updated += 1
        elif action in ("skipped", "dry-run"):
            self.skipped += 1
        elif action == "error":
            self.errors += 1
            if detail:
                self.error_detail.append(detail)

def _is_module(obj: dict) -> bool:
    if "TopicType" in obj:
        return False
    if any(h in obj for h in _MODULE_HINTS):
        return True
    
    return obj.get("Type") in (0, "0", "Module")

@dataclass
class _Ctx:
    force: bool
    dry_run: bool
    video_opts: "media.VideoOpts | None"
    transcribe_opts: "TranscribeOpts | None"
    base_url: str


def sync_content(
    bs: Brightspace,
    course: Course,
    course_dir: Path,
    manifest: Manifest,
    *,
    force: bool,
    dry_run: bool,
    video_opts: "media.VideoOpts | None" = None,
    transcribe_opts: "TranscribeOpts | None" = None,
) -> Stats:
    """Walk the whole content tree and mirror it locally.

    Primary source is the 'unstable' TOC endpoint (for the rich-text within a module)
    Falls back to the classic per-module crawl if that endpoint isn't available.
    """
    stats = Stats()
    content_root = course_dir / "Content"
    ou = course.id
    ctx = _Ctx(force, dry_run, video_opts, transcribe_opts, bs.base)
    index: list[dict] = []

    # loadDescription=true is what makes the TOC return each *module's* rich
    # text; without it every module comes back with Description: null and a
    # text-only module (a schedule table, say) leaves nothing on disk.
    toc = bs.get_json(bs.le_unstable(f"/{ou}/content/toc"),
                      params={"loadDescription": "true"}, default=None)
    if isinstance(toc, dict) and isinstance(toc.get("Modules"), list):
        log.debug("using unstable TOC (%d root modules)", len(toc["Modules"]))
        for m in toc["Modules"]:
            node = _walk_toc(bs, ou, m, content_root, ["Content"], manifest, stats, ctx)
            if node:
                index.append(node)
    else:
        log.info("  content TOC endpoint unavailable - using the classic crawl "
                 "(module/topic descriptions may be missing)")
        roots = bs.get_json(bs.le(f"/{ou}/content/root/"), default=[]) or []
        for module in roots:
            node = _walk_module(bs, ou, module, content_root, ["Content"], manifest,
                                stats, ctx)
            if node:
                index.append(node)

    if not index:
        log.info("  no content modules visible")
    _write_index(course, content_root, index, dry_run=dry_run)
    return stats


# Table of Contents (TOC) based traversal -------------------
def _walk_toc(bs, ou, m, base_dir, path_parts, manifest, stats, ctx: _Ctx):
    mid = m.get("ModuleId")
    title = _html.unescape(m.get("Title") or "") or f"Module {mid}"
    here = path_parts + [slugify(title)]
    dir_here = base_dir / slugify(title)
    subs = m.get("Modules") or []
    topics = m.get("Topics") or []
    last_mod = m.get("LastModifiedDate")

    log.info("  module: %s  (%d topics, %d submodules)",
             "/".join(here[1:]) or title, len(topics), len(subs))

    node: dict = {
        "type": "module", "id": mid, "title": title, "path": "/".join(here),
        "last_modified": last_mod, "hidden": bool(m.get("IsHidden")),
        "description": None, "children": [],
    }
    node["description"] = _save_description(
        bs, ou, m.get("Description"), dir_here=dir_here, base_name="_module",
        manifest=manifest, key=f"moddesc:{mid}", last_modified=last_mod,
        ctx=ctx, stats=stats, label=title)

    for sm in subs:
        c = _walk_toc(bs, ou, sm, dir_here, here, manifest, stats, ctx)
        if c:
            node["children"].append(c)
    for t in topics:
        c = _handle_toc_topic(bs, ou, t, dir_here, here, manifest, stats, ctx)
        if c:
            node["children"].append(c)
    return node


def _handle_toc_topic(bs, ou, t, dir_here, path_parts, manifest, stats, ctx: _Ctx):
    tid = t.get("TopicId")
    if tid is None:
        return None
    title = _html.unescape(t.get("Title") or "") or f"Topic {tid}"
    typ = (t.get("TypeIdentifier") or "").lower()
    url = t.get("Url") or ""
    last_mod = t.get("LastModifiedDate")
    key = f"topic:{tid}"
    log.debug("  topic %s '%s' type=%s url=%s", tid, title, typ or "?", url)

    node: dict = {
        "type": "topic", "id": tid, "title": title, "topic_type": typ,
        "path": "/".join(path_parts + [title]), "last_modified": last_mod,
        "hidden": bool(t.get("IsHidden")), "source_url": url,
        "content_url": t.get("ContentUrl"), "description": None, "local_path": None,
    }
    node["description"] = _save_description(
        bs, ou, t.get("Description"), dir_here=dir_here,
        base_name=slugify(title, max_len=90) + " - notes", manifest=manifest,
        key=f"topicdesc:{tid}", last_modified=last_mod, ctx=ctx, stats=stats, label=title)

    is_link = (
        typ == "link"
        or url.startswith("/d2l/")
        or (url.startswith("http") and "/content/enforced/" not in url)
    )
    if is_link:
        target = url if url.startswith("http") else f"{ctx.base_url}{url}"
        local = _write_link(dir_here, title, target, dry_run=ctx.dry_run)
        node["local_path"] = str(local) if local else None
        stats.links += 1
        manifest.record(key, last_modified=last_mod, local=local or dir_here,
                        extra={"kind": "link", "url": target, "title": title})
        return node

    prefer = (url.split("?")[0].split("/")[-1] or None) if url else None
    fallback = slugify(title, max_len=100)
    res = download_to(
        bs, bs.le(f"/{ou}/content/topics/{tid}/file"), dest_dir=dir_here,
        fallback_name=fallback, manifest=manifest, key=key, last_modified=last_mod,
        force=ctx.force, dry_run=ctx.dry_run, prefer_name=prefer)
    if res.action == "error" and url and not url.startswith("http"):
        res = download_to(
            bs, url, dest_dir=dir_here, fallback_name=fallback, manifest=manifest,
            key=key, last_modified=last_mod, force=ctx.force, dry_run=ctx.dry_run,
            prefer_name=prefer)

    node["local_path"] = str(res.path)
    stats.add(res.action, f"{title}: {res.error}" if res.error else "")
    if res.action == "error":
        log.warning("  could not download topic %s (%s): %s", tid, title, res.error)
        return node
    manifest.entries.setdefault(key, {}).update({"kind": "file", "title": title})
    if Path(res.path).suffix.lower() in (".html", ".htm"):
        _handle_embed(res.path, tid, ou, node, manifest, stats, ctx,
                      title=title, last_mod=last_mod)
    return node


# Rich Text within a module -> local .md / .html + pulled images --------------
def _save_description(bs, ou, desc, *, dir_here: Path, base_name: str, manifest: Manifest,
                      key: str, last_modified, ctx: _Ctx, stats: Stats, label: str):
    desc = desc or {}
    html = (desc.get("Html") or "").strip()
    text = (desc.get("Text") or "").strip()
    if not html and not text:
        return None

    # The API's own Text field flattens a table to one cell per line, so when
    # there is a table let html_to_text render it as a Markdown table instead.
    body_text = html_to_text(html) if "<table" in html.lower() else (text or html_to_text(html))
    md_path = dir_here / f"{base_name}.md"
    html_path = dir_here / f"{base_name}.html"

    if ctx.dry_run:
        stats.descriptions += 1
        return {"text": body_text}

    # Unchanged since last run? Don't rewrite files or re-fetch images.
    if not ctx.force and manifest.unchanged(key, last_modified=last_modified):
        manifest.touch(key)
        manifest.touch_prefix(f"{key}:asset:")   # its images weren't re-fetched
        stats.descriptions += 1
        return {"text": body_text,
                "md": str(md_path) if md_path.exists() else None,
                "html": str(html_path) if html_path.exists() else None}

    dir_here.mkdir(parents=True, exist_ok=True)
    md_path.write_text(f"# {label}\n\n{body_text}\n", "utf-8")
    result = {"md": str(md_path), "text": body_text, "html": None}

    if html:
        page, n_assets = _pull_assets(html, dir_here, bs, ctx.base_url, manifest, key)
        stats.assets += n_assets
        html_path.write_text(_wrap_html(label, page, ctx.base_url), "utf-8")
        result["html"] = str(html_path)
        manifest.record(key, last_modified=last_modified, local=html_path,
                        extra={"kind": "description", "title": label})
    else:
        manifest.record(key, last_modified=last_modified, local=md_path,
                        extra={"kind": "description", "title": label})

    stats.descriptions += 1
    log.info("  text:   %s", label)
    return result


def _wrap_html(label: str, body: str, base_url: str) -> str:
    return (
        f'<!doctype html><meta charset="utf-8"><title>{_html.escape(label)}</title>'
        f'<base href="{_html.escape(base_url)}/">'
        "<style>body{font:15px/1.6 system-ui,-apple-system,sans-serif;margin:2rem;"
        "max-width:820px}img{max-width:100%;height:auto}</style>"
        f"<h1>{_html.escape(label)}</h1>\n{body}\n"
    )


def _pull_assets(html: str, dir_here: Path, bs, base_url: str, manifest: Manifest,
                 key_prefix: str) -> tuple[str, int]:
    """Download <img> sources that live on the Brightspace host into _assets/ and
    rewrite them to relative paths; make any others absolute so the page still
    renders online."""
    if len(html) > _MAX_DESC_HTML:
        return html, 0
    assets_dir = dir_here / "_assets"
    n = 0

    def repl(m: "re.Match[str]") -> str:
        # groups: pre = '<img ... src="', src = the url, post = '"...>'
        nonlocal n
        pre, src, post = m.group(1), m.group(2), m.group(3)
        s = _html.unescape(src).strip()
        if s.startswith("data:") or not s:
            return m.group(0)
        if s.startswith("//"):
            s = "https:" + s
        if s.startswith("/"):
            if n < _ASSET_CAP:
                name = _fetch_asset(bs, s, assets_dir, manifest, f"{key_prefix}:asset:{n}")
                if name:
                    n += 1
                    return f"{pre}_assets/{name}{post}"
            return f"{pre}{_html.escape(base_url + s, quote=True)}{post}"  # absolute fallback
        return m.group(0)  # external / already-absolute URL - leave as-is

    return _IMG_SRC_RE.sub(repl, html), n


def _fetch_asset(bs, path: str, dest_dir: Path, manifest: Manifest, key: str) -> str | None:
    try:
        r = bs._raw_get(path)
        if r.status_code != 200 or not r.content:
            log.debug("asset %s -> HTTP %s", path, r.status_code)
            return None
        name = slugify(unquote(path.split("?")[0].split("/")[-1]) or "image", max_len=80)
        if "." not in name:
            ext = mimetypes.guess_extension(
                (r.headers.get("Content-Type") or "").split(";")[0].strip()) or ".bin"
            name += ext
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _dedupe(dest_dir / name)
        dest.write_bytes(r.content)
        manifest.record(key, last_modified=None, local=dest, extra={"kind": "asset"})
        log.debug("  asset:  %s (%d bytes)", dest.name, len(r.content))
        return dest.name
    except Exception as exc:  # noqa: BLE001
        log.debug("asset fetch failed %s: %s", path, exc)
        return None


def _dedupe(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while (c := p.with_name(f"{p.stem} ({n}){p.suffix}")).exists():
        n += 1
    return c

def _walk_module(
    bs: Brightspace,
    ou: int,
    module: dict,
    base_dir: Path,
    path_parts: list[str],
    manifest: Manifest,
    stats: Stats,
    ctx: _Ctx,
) -> dict | None:
    """For each child of a module, determines if it's a topic or a module, and processes into a node accordingly."""
    mod_id = module.get("Id")
    title = module.get("Title") or module.get("ShortTitle") or f"Module {mod_id}"
    folder_name = slugify(title)
    here = path_parts + [folder_name]
    dir_here = base_dir / folder_name

    node: dict = {
        "type": "module",
        "id": mod_id,
        "title": title,
        "path": "/".join(here),
        "last_modified": module.get("LastModifiedDate"),
        "hidden": bool(module.get("IsHidden")),
        "children": [],
    }

    if mod_id is None:
        return node

    children = bs.get_json(bs.le(f"/{ou}/content/modules/{mod_id}/structure/"), default=[]) or []

    if not children and isinstance(module.get("Structure"), list):
        children = module["Structure"]

    for child in children:
        if _is_module(child):
            sub = _walk_module(bs, ou, child, dir_here, here, manifest, stats, ctx)
            if sub:
                node["children"].append(sub)
        else:
            topic_node = _handle_topic(bs, ou, child, dir_here, here, manifest,
                                       stats, ctx)
            if topic_node:
                node["children"].append(topic_node)
    return node

def _handle_topic(
    bs: Brightspace,
    ou: int,
    child: dict,
    dir_here: Path,
    path_parts: list[str],
    manifest: Manifest,
    stats: Stats,
    ctx: _Ctx,
) -> dict | None:
    topic_id = child.get("Id")
    if topic_id is None:
        return None

    topic = bs.get_json(bs.le(f"/{ou}/content/topics/{topic_id}"), default=None) or child
    title = topic.get("Title") or child.get("Title") or f"Topic {topic_id}"
    ttype = topic.get("TopicType", child.get("TopicType"))
    url = topic.get("Url") or ""
    last_mod = topic.get("LastModifiedDate") or child.get("LastModifiedDate")
    key = f"topic:{topic_id}"

    node: dict = {
        "type": "topic",
        "id": topic_id,
        "title": title,
        "topic_type": ttype,
        "path": "/".join(path_parts + [title]),
        "last_modified": last_mod,
        "hidden": bool(topic.get("IsHidden")),
        "source_url": url,
        "local_path": None,
    }

    # Link topic
    if ttype == TOPIC_LINK or (url.startswith("http") and ttype != TOPIC_FILE):
        local = _write_link(dir_here, title, url, dry_run=ctx.dry_run)
        node["local_path"] = str(local) if local else None
        stats.links += 1
        manifest.record(key, last_modified=last_mod, local=local or dir_here,
                        extra={"kind": "link", "url": url, "title": title})
        return node

    # File topic
    prefer = None
    if url:
        prefer = url.split("?")[0].split("/")[-1] or None
    fallback = slugify(title, max_len=100)
    res = download_to(
        bs,
        bs.le(f"/{ou}/content/topics/{topic_id}/file"),
        dest_dir=dir_here,
        fallback_name=fallback,
        manifest=manifest,
        key=key,
        last_modified=last_mod,
        force=ctx.force,
        dry_run=ctx.dry_run,
        prefer_name=prefer,
    )

    # Some legacy topics 404 on /file but resolve with raw Url path's
    if res.action == "error" and url and not url.startswith("http"):
        res = download_to(
            bs, url,
            dest_dir=dir_here, fallback_name=fallback, manifest=manifest, key=key,
            last_modified=last_mod, force=ctx.force, dry_run=ctx.dry_run,
            prefer_name=prefer,
        )

    node["local_path"] = str(res.path)
    stats.add(res.action, f"{title}: {res.error}" if res.error else "")
    if res.action == "error":
        log.warning("  could not download topic %s (%s): %s", topic_id, title, res.error)
    manifest.entries.setdefault(key, {}).update({"kind": "file", "title": title})

    if res.action != "error" and Path(res.path).suffix.lower() in (".html", ".htm"):
        _handle_embed(res.path, topic_id, ou, node, manifest, stats, ctx,
                      title=title, last_mod=last_mod)
    return node
class _VideoProgress:
    """Throttled progress logger for the yt-dlp API path."""
    def __init__(self, title: str):
        self.title = title
        self._last = 0.0

    def __call__(self, d: dict) -> None:
        if d.get("status") != "downloading":
            return
        import time
        now = time.time()
        if now - self._last < 5:
            return
        self._last = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        got = d.get("downloaded_bytes", 0)
        if total:
            from .util import human_size
            log.info("    %s  %d%% of %s", self.title, int(100 * got / total),
                     human_size(total))


def _handle_embed(html_path, topic_id, ou, node, manifest, stats, ctx: _Ctx, *,
                  title, last_mod) -> None:
    """If a downloaded .html topic is just an embedded player, make it usable:
    write a .url pointer, rewrite the page for online viewing, and (if enabled)
    download the actual media file and/or its transcript."""
    base_url, video_opts, dry_run = ctx.base_url, ctx.video_opts, ctx.dry_run
    p = Path(html_path)
    try:
        raw = p.read_text("utf-8", errors="replace")
    except OSError:
        return
    if len(raw) > 1_000_000:
        return
    info = media.extract(raw)
    if not info:
        return

    stream_page = f"{base_url}/d2l/le/content/{ou}/viewContent/{topic_id}/View"
    node["media"] = {
        "provider": info.provider,
        "entry_id": info.entry_id,
        "embed_url": info.embed_url,
        "stream_page": stream_page,
        "video_file": None,
        "transcript_file": None,
    }
    stats.embeds += 1
    if dry_run:
        return

    media.write_pointer(p.with_suffix(".url"), stream_page)
    p.write_text(media.rewrite_html(info, base_url=base_url, stream_page=stream_page,
                                    title=title), "utf-8")
    manifest.record(f"topic:{topic_id}", last_modified=last_mod, local=p,
                    extra={"kind": "embed", "provider": info.provider, "title": title})

    video_file: Path | None = None
    if video_opts and video_opts.enabled:
        vkey = f"video:{topic_id}"
        log.info("  video: %s ...", title)
        status, video_file = media.download_video(
            info, dest_dir=p.parent, base_name=p.stem, base_url=base_url,
            opts=video_opts, progress=_VideoProgress(title),
        )
        if video_file:
            node["media"]["video_file"] = video_file.name
            manifest.record(vkey, last_modified=last_mod, local=video_file,
                            extra={"kind": "video", "provider": info.provider})
            if status == "ok":
                stats.videos += 1
        elif status == "no-ytdlp":
            log.warning("  include.videos is on but yt-dlp is unavailable - reinstall "
                        "brightspace-sync (it should be bundled)")
        else:
            manifest.touch(vkey)  # keep any earlier copy safe from --prune
            stats.video_errors += 1
            log.warning("  video download failed for %s (%s)", title, status)

    _handle_transcript(info, p, topic_id, node, manifest, stats, ctx,
                       title=title, last_mod=last_mod, video_file=video_file)


def _handle_transcript(info, html_path: Path, topic_id, node, manifest, stats,
                       ctx: _Ctx, *, title, last_mod, video_file: Path | None) -> None:
    """Save the video's transcript as Markdown next to it (--transcribe)."""
    opts = ctx.transcribe_opts
    if not (opts and opts.enabled):
        return
    tkey = f"transcript:{topic_id}"
    log.info("  transcript: %s ...", title)
    status, path = transcribe(
        info, dest_dir=html_path.parent, base_name=html_path.stem, title=title,
        base_url=ctx.base_url, video_path=video_file, opts=opts, force=ctx.force,
    )
    if path:
        node["media"]["transcript_file"] = path.name
        manifest.record(tkey, last_modified=last_mod, local=path,
                        extra={"kind": "transcript", "source": status})
        if status in ("captions", "asr"):
            stats.transcripts += 1
        return

    manifest.touch(tkey)  # keep any earlier transcript safe from --prune
    if status == "no-whisper":
        log.warning("  no captions for %s, and local transcription is unavailable "
                    "- install it with: pip install \"brightspace-sync[transcribe]\"",
                    title)
    elif status == "no-captions":
        log.info("  no transcript available for %s", title)
    else:
        stats.transcript_errors += 1
        log.warning("  transcript failed for %s (%s)", title, status)


def _write_link(dir_here: Path, title: str, url: str, *, dry_run: bool) -> Path | None:
    dest = dir_here / (slugify(title, max_len=100) + ".url")
    if dry_run:
        return dest
    dir_here.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"[InternetShortcut]\nURL={url}\n", "utf-8")
    return dest


def _rel(value, base: Path):
    if not value:
        return value
    p = Path(value)
    if not p.is_absolute():
        return value
    try:
        return str(p.relative_to(base))
    except ValueError:
        return value


def _relativize(nodes: list[dict], base: Path) -> None:
    """Rewrite stored file paths to be relative to the course folder, so the
    whole folder stays portable (renamable / movable)."""
    for n in nodes:
        n["local_path"] = _rel(n.get("local_path"), base)
        d = n.get("description")
        if isinstance(d, dict):
            d["html"] = _rel(d.get("html"), base)
            d["md"] = _rel(d.get("md"), base)
        _relativize(n.get("children", []), base)


def _write_index(course: Course, content_root: Path, index: list[dict], *, dry_run: bool) -> None:
    if dry_run:
        return
    content_root.mkdir(parents=True, exist_ok=True)
    _relativize(index, content_root.parent)
    payload = {
        "course": {"id": course.id, "name": course.name, "code": course.code},
        "generated_at": now_iso(),
        "modules": index,
    }
    (content_root / "content_index.json").write_text(
        json.dumps(payload, indent=2), "utf-8"
    )
    (content_root / "content_index.md").write_text(_render_md(course, index), "utf-8")


def _render_md(course: Course, index: list[dict], depth: int = 0) -> str:
    if depth == 0:
        head = f"# {course.code or ''} {course.name}\n\n_Content outline - generated by brightspace_sync_\n\n"
        return head + "".join(_render_md_node(n, 0) for n in index)
    return "".join(_render_md_node(n, depth) for n in index)


def _desc_block(node: dict, pad: str) -> str:
    d = node.get("description")
    if not isinstance(d, dict) or not d.get("text"):
        return ""
    body = "\n".join(f"{pad}  > {ln}" if ln else f"{pad}  >"
                     for ln in d["text"].splitlines())
    return body + "\n"


def _render_md_node(node: dict, depth: int) -> str:
    pad = "  " * depth
    if node["type"] == "module":
        line = f"{pad}- **{node['title']}**"
        if node.get("hidden"):
            line += " _(hidden)_"
        line += "\n"
        line += _desc_block(node, pad)
        return line + "".join(_render_md_node(c, depth + 1) for c in node["children"])
    # topic
    label = node["title"]
    lp = node.get("local_path")
    tt = node.get("topic_type")
    if tt == TOPIC_LINK or tt == "link":
        line = f"{pad}- [{label}]({node.get('source_url', '')}) (link)\n"
    elif lp:
        line = f"{pad}- {label} -> `{Path(lp).name}`\n"
    else:
        line = f"{pad}- {label} _(not downloaded)_\n"
    return line + _desc_block(node, pad)
