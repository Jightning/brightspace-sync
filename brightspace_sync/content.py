"""
Traverses a course's Content tree

A course's content is split into modules, with each one having either a child module or a topic.
Topics are either files or links

Output layout under <course folder>/Content/:
    <Module>/<Submodule>/<Topic Title>.<ext>      # file topics
    <Module>/<Submodule>/<Topic Title>.url        # link topics (INI shortcut)
Plus:
    content_index.json   machine-readable tree with local paths + metadata
    content_index.md     human-readable outline
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import media
from .client import Brightspace
from .download import download_to
from .enrollments import Course
from .manifest import Manifest
from .util import log, now_iso, slugify

TOPIC_FILE = 1
TOPIC_LINK = 3

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

def sync_content(
    bs: Brightspace,
    course: Course,
    course_dir: Path,
    manifest: Manifest,
    *,
    force: bool,
    dry_run: bool,
    video_opts: "media.VideoOpts | None" = None,
) -> Stats:
    """
    Fetches root modules from the Brightspace API.
    For each module, it recursively walks the tree, collects all nodes into an index list, and write into index files.
    """
    stats = Stats()
    content_root = course_dir / "Content"
    ou = course.id

    roots = bs.get_json(bs.le(f"/{ou}/content/root/"), default=[]) or []
    if not roots:
        log.info("  no content modules visible")
        return stats

    index: list[dict] = []
    for module in roots:
        node = _walk_module(bs, ou, module, content_root, ["Content"], manifest,
                            stats, force=force, dry_run=dry_run, video_opts=video_opts)
        if node:
            index.append(node)

    _write_index(course, content_root, index, dry_run=dry_run)
    return stats

def _walk_module(
    bs: Brightspace,
    ou: int,
    module: dict,
    base_dir: Path,
    path_parts: list[str],
    manifest: Manifest,
    stats: Stats,
    *,
    force: bool,
    dry_run: bool,
    video_opts: "media.VideoOpts | None" = None,
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
            sub = _walk_module(bs, ou, child, dir_here, here, manifest, stats,
                               force=force, dry_run=dry_run, video_opts=video_opts)
            if sub:
                node["children"].append(sub)
        else:
            topic_node = _handle_topic(bs, ou, child, dir_here, here, manifest,
                                       stats, force=force, dry_run=dry_run,
                                       video_opts=video_opts)
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
    *,
    force: bool,
    dry_run: bool,
    video_opts: "media.VideoOpts | None" = None,
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
        local = _write_link(dir_here, title, url, dry_run=dry_run)
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
        force=force,
        dry_run=dry_run,
        prefer_name=prefer,
    )

    # Some legacy topics 404 on /file but resolve with raw Url path's
    if res.action == "error" and url and not url.startswith("http"):
        res = download_to(
            bs, url,
            dest_dir=dir_here, fallback_name=fallback, manifest=manifest, key=key,
            last_modified=last_mod, force=force, dry_run=dry_run, prefer_name=prefer,
        )

    node["local_path"] = str(res.path)
    stats.add(res.action, f"{title}: {res.error}" if res.error else "")
    if res.action == "error":
        log.warning("  could not download topic %s (%s): %s", topic_id, title, res.error)
    manifest.entries.setdefault(key, {}).update({"kind": "file", "title": title})

    if res.action != "error" and Path(res.path).suffix.lower() in (".html", ".htm"):
        _handle_embed(res.path, topic_id, ou, bs.base, node, manifest, stats,
                      title=title, last_mod=last_mod, video_opts=video_opts,
                      dry_run=dry_run)
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


def _handle_embed(html_path, topic_id, ou, base_url, node, manifest, stats, *,
                  title, last_mod, video_opts, dry_run) -> None:
    """If a downloaded .html topic is just an embedded player, make it usable:
    write a .url pointer, rewrite the page for online viewing, and (if enabled)
    download the actual media file."""
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
    }
    stats.embeds += 1
    if dry_run:
        return

    media.write_pointer(p.with_suffix(".url"), stream_page)
    p.write_text(media.rewrite_html(info, base_url=base_url, stream_page=stream_page,
                                    title=title), "utf-8")
    manifest.record(f"topic:{topic_id}", last_modified=last_mod, local=p,
                    extra={"kind": "embed", "provider": info.provider, "title": title})

    if not (video_opts and video_opts.enabled):
        return
    vkey = f"video:{topic_id}"
    log.info("  video: %s ...", title)
    status, path = media.download_video(
        info, dest_dir=p.parent, base_name=p.stem, base_url=base_url,
        opts=video_opts, progress=_VideoProgress(title),
    )
    if path:
        node["media"]["video_file"] = path.name
        manifest.record(vkey, last_modified=last_mod, local=path,
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


def _write_link(dir_here: Path, title: str, url: str, *, dry_run: bool) -> Path | None:
    dest = dir_here / (slugify(title, max_len=100) + ".url")
    if dry_run:
        return dest
    dir_here.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"[InternetShortcut]\nURL={url}\n", "utf-8")
    return dest


def _relativize(nodes: list[dict], base: Path) -> None:
    """Rewrite every node's local_path to be relative to the course folder, so
    the whole folder stays portable (renamable / movable)."""
    for n in nodes:
        lp = n.get("local_path")
        if lp:
            p = Path(lp)
            try:
                n["local_path"] = str(p.relative_to(base)) if p.is_absolute() else lp
            except ValueError:
                pass
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


def _render_md_node(node: dict, depth: int) -> str:
    pad = "  " * depth
    if node["type"] == "module":
        line = f"{pad}- **{node['title']}**"
        if node.get("hidden"):
            line += " _(hidden)_"
        line += "\n"
        return line + "".join(_render_md_node(c, depth + 1) for c in node["children"])
    # topic
    label = node["title"]
    lp = node.get("local_path")
    if node.get("topic_type") == TOPIC_LINK:
        return f"{pad}- [{label}]({node.get('source_url', '')}) (link)\n"
    if lp:
        rel = Path(lp).name
        return f"{pad}- {label} -> `{rel}`\n"
    return f"{pad}- {label} _(not downloaded)_\n"
