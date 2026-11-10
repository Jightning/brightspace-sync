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
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, quote

from . import media
from .client import Brightspace
from .download import download_to
from .enrollments import Course
from .manifest import Manifest
from .navigation import navigation_links
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
    external_files: int = 0
    assets: int = 0         # images pulled out of that rich text
    error_detail: list[str] = field(default_factory=list)

    def add(self, action: str, detail: str = "") -> None:
        if action == "downloaded":
            self.downloaded += 1
        elif action == "updated":
            self.updated += 1
        elif action in ("skipped", "dry-run", "declined"):
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
    if not getattr(getattr(bs, 'external_downloads', None), 'enabled', False):
        manifest.touch_prefix('external:')
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

    overview = _sync_overview(bs, ou, content_root, manifest, stats, ctx)
    if overview:
        index.insert(0, overview)
    navigation = _sync_navigation(bs, ou, content_root, manifest, stats, ctx)
    if navigation:
        index.append(navigation)

    if not index:
        log.info("  no content modules visible")
    _write_index(course, content_root, index, dry_run=dry_run, base_url=bs.base)
    return stats


def _sync_navigation(bs, ou, content_root, manifest, stats, ctx):
    page_url = f"{ctx.base_url}/d2l/le/content/{ou}/Home"
    response = bs._raw_get(page_url, allow_forbidden=True)
    try:
        if response.status_code in (403, 404):
            log.warning("  course navigation unavailable (HTTP %s); keeping earlier shortcuts",
                        response.status_code)
            manifest.touch_prefix("navigation:")
            manifest.touch_prefix("external:")
            return None
        response.raise_for_status()
        links = navigation_links(response.text, page_url)
    finally:
        response.close()
    if not links:
        log.warning("  no course navigation links found; keeping earlier shortcuts")
        manifest.touch_prefix("navigation:")
        manifest.touch_prefix("external:")
        return None
    children = []
    for title, url in links:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        # The suffix also prevents two tools with the same title overwriting each other.
        local = _write_link(content_root / "Course links", f"{title} - {digest}",
                            url, dry_run=ctx.dry_run)
        manifest.record(f"navigation:{digest}", last_modified=None, local=local,
                        extra={"kind": "link", "title": title, "url": url})
        node = {"type": "topic", "title": title, "topic_type": "link",
                "source_url": url, "local_path": str(local)}
        _download_online(bs, node, content_root / "Course links", manifest, stats, ctx)
        children.append(node)
        stats.links += 1
    return {"type": "module", "title": "Course links", "children": children}


def _download_online(bs, node, directory, manifest, stats, ctx):
    downloader = getattr(bs, 'external_downloads', None)
    if downloader is None:
        return
    path = downloader.fetch(node.get('source_url', ''), node['title'], directory,
                            manifest, force=ctx.force, dry_run=ctx.dry_run)
    if path:
        node['offline_path'] = str(path)
        stats.external_files += 1


def _sync_overview(bs, ou, content_root, manifest, stats, ctx):
    response = bs._raw_get(bs.le(f"/{ou}/overview"), allow_forbidden=True)
    try:
        if response.status_code == 404:
            return None
        if response.status_code == 403:
            log.warning("  course overview is not permitted; keeping earlier files")
            manifest.touch_prefix("overview:")
            return None
        response.raise_for_status()
        overview = response.json()
    finally:
        response.close()
    if not isinstance(overview, dict):
        raise ValueError("course overview must be a JSON object")
    directory = content_root / "Course overview"
    # Overview has no modification date, so re-fetch rather than caching forever.
    fresh = _Ctx(True, ctx.dry_run, ctx.video_opts, ctx.transcribe_opts, ctx.base_url)
    description = _save_description(
        bs, ou, overview.get("Description"), dir_here=directory, base_name="Overview",
        manifest=manifest, key="overview:description", last_modified=None,
        ctx=fresh, stats=stats, label="Course overview")
    if description and not ctx.dry_run:
        manifest.record("overview:markdown", last_modified=None,
                        local=directory / "Overview.md")
    children = []
    if overview.get("HasAttachment"):
        source = bs.le(f"/{ou}/overview/attachment")
        result = download_to(bs, source, dest_dir=directory,
                             fallback_name="Overview attachment", manifest=manifest,
                             key="overview:attachment", last_modified=None,
                             force=True, dry_run=ctx.dry_run)
        stats.add(result.action, f"Overview attachment: {result.error}" if result.error else "")
        children.append({"type": "topic", "title": "Overview attachment",
                         "local_path": str(result.path) if result.action != "error" else None,
                         "source_url": ctx.base_url + source})
    return {"type": "module", "title": "Course overview", "description": description,
            "children": children}


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
        _download_online(bs, node, dir_here, manifest, stats, ctx)
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
    if res.action == "declined":
        return node
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
    if (not ctx.force and manifest.unchanged(key, last_modified=last_modified)
            and not (manifest.get(key) or {}).get('retry_assets')):
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
        page, n_assets, declined = _pull_assets(html, dir_here, bs, ctx.base_url, manifest, key)
        stats.assets += n_assets
        html_path.write_text(_wrap_html(label, page, ctx.base_url), "utf-8")
        result["html"] = str(html_path)
        manifest.record(key, last_modified=last_modified, local=html_path,
                        extra={"kind": "description", "title": label, "retry_assets": declined})
    else:
        manifest.record(key, last_modified=last_modified, local=md_path,
                        extra={"kind": "description", "title": label})

    stats.descriptions += 1
    log.info("  text:   %s", label)
    return result


def _wrap_html(label: str, body: str, base_url: str) -> str:
    # A remote <base> makes even downloaded _assets resolve online. Keep local
    # images relative, and resolve document links individually instead.
    body = re.sub(r'''(?i)(\bhref\s*=\s*)(["'])(.*?)\2''',
                  lambda m: m[1] + m[2] + _html.escape(
                      urljoin(base_url + "/", _html.unescape(m[3])), quote=True) + m[2], body)
    return (
        f'<!doctype html><meta charset="utf-8"><title>{_html.escape(label)}</title>'
        "<style>body{font:15px/1.6 system-ui,-apple-system,sans-serif;margin:2rem;"
        "max-width:820px}img{max-width:100%;height:auto}</style>"
        f"<h1>{_html.escape(label)}</h1>\n{body}\n"
    )


def _pull_assets(html: str, dir_here: Path, bs, base_url: str, manifest: Manifest,
                 key_prefix: str) -> tuple[str, int, bool]:
    """Download <img> sources that live on the Brightspace host into _assets/ and
    rewrite them to relative paths; make any others absolute so the page still
    renders online."""
    if len(html) > _MAX_DESC_HTML:
        return html, 0, False
    assets_dir = dir_here / "_assets"
    n = 0
    declined = False

    def repl(m: "re.Match[str]") -> str:
        # groups: pre = '<img ... src="', src = the url, post = '"...>'
        nonlocal n, declined
        pre, src, post = m.group(1), m.group(2), m.group(3)
        s = _html.unescape(src).strip()
        if s.startswith("data:") or not s:
            return m.group(0)
        if s.startswith("//"):
            s = "https:" + s
        if s.startswith("/"):
            if n < _ASSET_CAP:
                name, skipped = _fetch_asset(bs, s, assets_dir, manifest, f"{key_prefix}:asset:{n}")
                declined = declined or skipped
                if name:
                    n += 1
                    return f"{pre}_assets/{name}{post}"
            return f"{pre}{_html.escape(base_url + s, quote=True)}{post}"  # absolute fallback
        return m.group(0)  # external / already-absolute URL - leave as-is

    page = _IMG_SRC_RE.sub(repl, html)
    return page, n, declined


def _fetch_asset(bs, path: str, dest_dir: Path, manifest: Manifest, key: str) -> tuple[str | None, bool]:
    try:
        name = slugify(unquote(path.split("?")[0].split("/")[-1]) or "image", max_len=80)
        if "." not in name:
            name += ".bin"
        result = download_to(bs, path, dest_dir=dest_dir, fallback_name=name,
                             manifest=manifest, key=key, last_modified=None,
                             force=True, dry_run=False, prefer_name=name)
        if result.action in ('error', 'declined'):
            return None, result.action == 'declined'
        manifest.entries[key]['kind'] = 'asset'
        return result.path.name, False
    except Exception as exc:  # noqa: BLE001
        log.debug("asset fetch failed %s: %s", path, exc)
        return None, False


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
        _download_online(bs, node, dir_here, manifest, stats, ctx)
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
    if res.action == "declined":
        return node
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
        elif status == "declined":
            manifest.touch(vkey)
            stats.skipped += 1
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
    if status == "declined":
        stats.skipped += 1
    elif status == "no-whisper":
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
        if n.get('offline_path'):
            n['offline_path'] = _rel(n['offline_path'], base)
            if not (base / n['offline_path']).is_file():
                n['offline_path'] = None
        n["local_path"] = _rel(n.get("local_path"), base)
        # Older indexes can retain the intended filename after a failed download.
        # Offer the source link instead of a broken local-file link.
        if n.get("local_path") and not (base / n["local_path"]).is_file():
            n["local_path"] = None
        d = n.get("description")
        if isinstance(d, dict):
            d["html"] = _rel(d.get("html"), base)
            d["md"] = _rel(d.get("md"), base)
        _relativize(n.get("children", []), base)


def _write_index(course: Course, content_root: Path, index: list[dict], *, dry_run: bool,
                 base_url: str = "") -> None:
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
    (content_root / "index.html").write_text(_render_html(course, index, base_url), "utf-8")


def _render_html(course: Course, nodes: list[dict], base_url: str) -> str:
    def local_href(path):
        # Stored paths are relative to the course folder; this page is in Content/.
        return "../" + quote(str(path).replace("\\", "/"), safe="/")

    def render(node):
        title = _html.escape(node["title"])
        source = urljoin(base_url + "/", node.get("source_url") or "") if node.get("source_url") else None
        href = source if node.get("topic_type") in (TOPIC_LINK, "link") else None
        if node.get("local_path") and not href:
            href = local_href(node["local_path"])
        href = href or source
        if node.get('offline_path'):
            href = local_href(node['offline_path'])
        label = f'<a href="{_html.escape(href, quote=True)}">{title}</a>' if href else title
        if node.get('offline_path'):
            label += f' (offline) — <a href="{_html.escape(source or "", quote=True)}">Online source</a>'
        elif node.get("topic_type") in (TOPIC_LINK, "link") or (source and not node.get("local_path")):
            label += " (online)"
        desc = node.get("description") or {}
        if desc.get("html") or desc.get("md"):
            label += f' — <a href="{_html.escape(local_href(desc.get("html") or desc["md"]), quote=True)}">Read saved text</a>'
        return "<li>" + label + ("<ul>" + "".join(render(c) for c in node["children"]) + "</ul>"
                                  if node.get("children") else "") + "</li>"

    return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>Course content</title><style>body{font:16px/1.6 system-ui;margin:2rem;max-width:70rem}'
            'li{margin:.4rem 0}a{overflow-wrap:anywhere}</style>'
            f'<h1>{_html.escape(course.name)}</h1><p>Saved files open offline. Links marked online '
            'require an internet connection and may require Brightspace sign-in.</p><ul>'
            + "".join(render(n) for n in nodes) + "</ul>")


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
    if node.get('offline_path'):
        line = f"{pad}- {label} -> `{node['offline_path']}` (offline); [online source]({node.get('source_url', '')})\n"
    elif tt == TOPIC_LINK or tt == "link":
        line = f"{pad}- [{label}]({node.get('source_url', '')}) (link)\n"
    elif lp:
        line = f"{pad}- {label} -> `{Path(lp).name}`\n"
    elif node.get("source_url"):
        line = f"{pad}- [{label}]({node['source_url']}) _(online fallback)_\n"
    else:
        line = f"{pad}- {label} _(not downloaded)_\n"
    return line + _desc_block(node, pad)
