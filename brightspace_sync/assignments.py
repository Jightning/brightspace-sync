"""Assignment (Dropbox) folders -> Assignments/<name>/instructions.md + files."""

from __future__ import annotations

from pathlib import Path

from .client import Brightspace
from .download import download_to
from .enrollments import Course
from .manifest import Manifest
from .news import _html_to_text
from .util import log, slugify


def sync_assignments(
    bs: Brightspace,
    course: Course,
    course_dir: Path,
    manifest: Manifest,
    *,
    force: bool,
    dry_run: bool,
) -> int:
    ou = course.id
    folders = bs.get_json(bs.le(f"/{ou}/dropbox/folders/"), default=[]) or []
    if not folders:
        log.info("  no assignment folders")
        return 0

    base = course_dir / "Assignments"
    count = 0
    for f in folders:
        fid = f.get("Id")
        if fid is None:
            continue
        name = f.get("Name") or f"Assignment {fid}"
        folder = base / slugify(name, max_len=100)
        instr = f.get("CustomInstructions") or {}
        text = instr.get("Text") or _html_to_text(instr.get("Html", ""))
        due = f.get("DueDate")
        last_mod = f.get("LastModifiedDate") or due or ""
        key = f"dropbox:{fid}"

        if not dry_run:
            folder.mkdir(parents=True, exist_ok=True)
            md = (
                f"# {name}\n\n"
                f"- Due: {due or 'n/a'}\n"
                f"- Folder id: {fid}\n"
                f"- Group submission: {f.get('GroupTypeId') is not None}\n\n"
                f"---\n\n{text or '_No instructions text._'}\n"
            )
            (folder / "instructions.md").write_text(md, "utf-8")
        manifest.record(key, last_modified=last_mod, local=folder / "instructions.md",
                        extra={"kind": "assignment", "title": name, "due": due})
        count += 1

        for att in f.get("Attachments") or []:
            aid = att.get("FileId")
            if aid is None:
                continue
            download_to(
                bs,
                bs.le(f"/{ou}/dropbox/folders/{fid}/attachments/{aid}"),
                dest_dir=folder,
                fallback_name=slugify(att.get("FileName") or f"attachment-{aid}"),
                manifest=manifest,
                key=f"dropbox:{fid}:att:{aid}",
                last_modified=last_mod,
                force=force,
                dry_run=dry_run,
                prefer_name=att.get("FileName"),
            )

    log.info("  %d assignment folders", count)
    return count
