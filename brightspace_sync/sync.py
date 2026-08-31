"""authenticate, picks courses, then sync each section."""

from __future__ import annotations

import json
from pathlib import Path

from .assignments import sync_assignments
from .auth import AuthError, build_session
from .calendar_events import sync_calendar
from .client import AuthExpired, Brightspace
from .config import Config
from .content import sync_content
from .enrollments import Course, list_all_courses, select_courses
from .grades import sync_grades
from .manifest import Manifest
from .mapping import resolve_placements
from .media import VideoOpts
from .news import sync_news
from .util import log, now_iso

class SyncOutcome:
    """Output for the sync"""
    def __init__(self) -> None:
        self.courses: list[str] = []
        self.auth_failed = False
        self.errors: list[str] = []
        self.file_errors = 0
        self.pruned = 0

    @property
    def ok(self) -> bool:
        return not self.auth_failed and not self.errors

def run_sync(
    cfg: Config,
    *,
    cli_course_ids: list[int] | None = None,
    only: set[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    prune: bool = False,
    skip_videos: bool = False,
) -> SyncOutcome:
    out = SyncOutcome()
    do_prune = prune or cfg.prune

    # Authenticate
    try:
        session, whoami = build_session(cfg)
    except AuthError as exc:
        out.auth_failed = True
        out.errors.append(str(exc))
        log.error("%s", exc)
        return out

    bs = Brightspace(session, cfg)

    # Pick Courses
    all_courses = list_all_courses(bs)
    log.info("Enrolled in %d course offerings", len(all_courses))
    courses = select_courses(cfg, all_courses, cli_course_ids)
    if not courses:
        log.warning("No courses matched the current filter. Nothing to do.")
        return out

    placements = resolve_placements(cfg, courses)
    sections = only or {k for k, v in cfg.include.items() if v}

    log.info("%sWriting to: %s", "[dry-run] " if dry_run else "", cfg.courses_dir)
    if do_prune and only:
        log.warning("--prune with --only: will only prune the section(s) %s",
                    ", ".join(sorted(sections)))

    if not dry_run:
        cfg.courses_dir.mkdir(parents=True, exist_ok=True)
        _write_root_index(cfg.courses_dir, whoami, courses, all_courses, placements)

    # Sync Each Course
    for course in courses:
        pl = placements[course.id]
        cdir = cfg.courses_dir.joinpath(*pl.parts())
        tag = pl.folder + (f"/{pl.subfolder}" if pl.subfolder else "")
        log.info("=== %s | %s (id %s) -> %s%s ===",
                 course.code or "no-code", course.name, course.id, tag,
                 f"  [merged: {pl.merged_with} courses]" if pl.merged_with > 1 else "")
        manifest = Manifest.load(cdir)

        if not dry_run:
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "course.json").write_text(json.dumps({
                "id": course.id, "name": course.name, "code": course.code,
                "is_active": course.is_active, "synced_at": now_iso(),
            }, indent=2), "utf-8")

        want_videos = cfg.includes_section("videos") and not skip_videos
        vopts = VideoOpts(
            enabled=want_videos,
            rate_limit=cfg.video_rate_limit,
            max_height=cfg.video_quality,
            timeout=cfg.video_timeout,
            archive=cdir / ".video-archive.txt",
        )

        try:
            if "content" in sections and cfg.includes_section("content"):
                st = sync_content(bs, course, cdir, manifest, force=force, dry_run=dry_run, video_opts=vopts)

                log.info("  content: %d new, %d updated, %d unchanged, %d links, "
                        "%d embeds, %d videos, %d errors",
                        st.downloaded, st.updated, st.skipped, st.links,
                        st.embeds, st.videos, st.errors + st.video_errors)
                out.file_errors += st.errors + st.video_errors
                out.errors.extend(st.error_detail)
            if "grades" in sections and cfg.includes_section("grades"):
                sync_grades(bs, course, cdir, manifest, dry_run=dry_run)
            if "news" in sections and cfg.includes_section("news"):
                sync_news(bs, course, cdir, manifest, force=force, dry_run=dry_run)
            if "assignments" in sections and cfg.includes_section("assignments"):
                sync_assignments(bs, course, cdir, manifest, force=force, dry_run=dry_run)
            if "calendar" in sections and cfg.includes_section("calendar"):
                sync_calendar(bs, course, cdir, manifest, dry_run=dry_run)

            if do_prune:
                prune_sections = set(sections) | ({"videos"} if want_videos else set())
                gone = manifest.prune(prune_sections, dry_run=dry_run)
                for p in gone:
                    log.info("  %s %s", "would remove" if dry_run else "removed",
                             p.name)
                out.pruned += len(gone)
        except AuthExpired as exc:
            out.auth_failed = True
            out.errors.append(str(exc))
            log.error("%s", exc)
            log.error("Stopping - progress so far is saved (see the `finally` "
                      "below); re-run after signing back into Brightspace.")
            break
        except Exception as exc:  # noqa: BLE001 - keep going with other courses
            log.exception("  course %s failed: %s", course.id, exc)
            out.errors.append(f"{course.code or course.id}: {exc}")
        finally:
            if not dry_run:
                manifest.save()

        out.courses.append(f"{course.code or course.id} - {course.name}")

    log.info("Done. %d course(s) into %s", len(out.courses), cfg.courses_dir)
    if out.file_errors:
        log.warning("%d file(s) could not be downloaded (see content_index.json)",
                    out.file_errors)
    if out.pruned:
        log.info("%s %d local file(s) that are gone from Brightspace",
                 "would prune" if dry_run else "pruned", out.pruned)
    return out


def _write_root_index(root: Path, whoami: dict, selected, all_courses, placements) -> None:
    payload = {
        "generated_at": now_iso(),
        "user": {
            "name": (whoami.get("FirstName", "") + " " + whoami.get("LastName", "")).strip(),
            "unique_name": whoami.get("UniqueName"),
            "id": whoami.get("Identifier"),
        },
        "synced_courses": [
            {"id": c.id, "name": c.name, "code": c.code,
             "folder": "/".join(placements[c.id].parts()),
             "merged": placements[c.id].merged_with > 1}
            for c in selected
        ],
        "all_enrollments": [
            {"id": c.id, "name": c.name, "code": c.code, "active": c.is_active}
            for c in all_courses
        ],
    }

    (root / "index.json").write_text(json.dumps(payload, indent=2), "utf-8")
