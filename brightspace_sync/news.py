"""Course announcements (News) -> Announcements/*.md + attachments."""

from __future__ import annotations

import html
import re
from pathlib import Path

from .client import Brightspace
from .download import download_to
from .enrollments import Course
from .manifest import Manifest
from .util import log, parse_dt, slugify

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _html_to_text(markup: str) -> str:
    if not markup:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", markup)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    return _WS.sub("\n\n", text).strip()


def sync_news(
    bs: Brightspace,
    course: Course,
    course_dir: Path,
    manifest: Manifest,
    *,
    force: bool,
    dry_run: bool,
) -> int:
    ou = course.id
    items = bs.get_json(bs.le(f"/{ou}/news/"), default=[]) or []
    if not items:
        log.info("  no announcements")
        return 0

    out_dir = course_dir / "Announcements"
    count = 0
    for item in items:
        nid = item.get("Id")
        if nid is None:
            continue
        title = item.get("Title") or f"Announcement {nid}"
        created = item.get("CreationDate") or item.get("StartDate") or ""
        dt = parse_dt(created)
        prefix = dt.strftime("%Y-%m-%d") if dt else "undated"
        fname = f"{prefix} - {slugify(title, max_len=90)}.md"
        md_path = out_dir / fname
        body = item.get("Body") or {}
        text = body.get("Text") or _html_to_text(body.get("Html", ""))

        key = f"news:{nid}"
        last_mod = item.get("LastModifiedDate") or created

        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            md = (
                f"# {title}\n\n"
                f"- Posted: {created or 'unknown'}\n"
                f"- Announcement id: {nid}\n\n---\n\n{text}\n"
            )
            md_path.write_text(md, "utf-8")
        manifest.record(key, last_modified=last_mod, local=md_path,
                        extra={"kind": "news", "title": title})
        count += 1

        for att in item.get("Attachments") or []:
            fid = att.get("FileId")
            if fid is None:
                continue
            akey = f"news:{nid}:att:{fid}"
            download_to(
                bs,
                bs.le(f"/{ou}/news/{nid}/attachments/{fid}"),
                dest_dir=out_dir / slugify(f"{prefix} {title}", max_len=90),
                fallback_name=slugify(att.get("FileName") or f"attachment-{fid}"),
                manifest=manifest,
                key=akey,
                last_modified=last_mod,
                force=force,
                dry_run=dry_run,
                prefer_name=att.get("FileName"),
            )

    log.info("  %d announcements", count)
    return count
