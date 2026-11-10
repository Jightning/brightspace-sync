"""Course calendar events -> calendar.json"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import Brightspace
from .enrollments import Course
from .manifest import Manifest
from .util import log, now_iso


def sync_calendar(
    bs: Brightspace,
    course: Course,
    course_dir: Path,
    manifest: Manifest,
    *,
    dry_run: bool,
) -> int:
    ou = course.id
    now = datetime.now(timezone.utc)
    params = {
        # Brightspace's UTCDateTime parser requires the milliseconds component.
        "startDateTime": (now - timedelta(days=120)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "endDateTime": (now + timedelta(days=240)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    events = bs.paged(bs.le(f"/{ou}/calendar/events/myEvents/"), params=params)
    rows = []
    for e in events:
        rows.append({
            "id": e.get("CalendarEventId"),
            "title": e.get("Title"),
            "description": (e.get("Description") or "").strip(),
            "start": e.get("StartDateTime"),
            "end": e.get("EndDateTime"),
            "all_day": e.get("IsAllDayEvent"),
            "location": e.get("LocationName"),
        })
    rows.sort(key=lambda r: r.get("start") or "")
    if not dry_run:
        (course_dir / "calendar.json").write_text(
            json.dumps({"course": {"id": course.id, "name": course.name},
                        "generated_at": now_iso(), "events": rows}, indent=2),
            "utf-8",
        )
    manifest.data["calendar_synced_at"] = now_iso()
    log.info("  %d calendar events", len(rows))
    return len(rows)
