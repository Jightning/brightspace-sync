"""Dump the user's own grade values for a course -> grades.json + grades.csv"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .client import Brightspace
from .enrollments import Course
from .manifest import Manifest
from .util import log, now_iso, sha256_bytes


def sync_grades(
    bs: Brightspace,
    course: Course,
    course_dir: Path,
    manifest: Manifest,
    *,
    dry_run: bool,
) -> str:
    ou = course.id
    values = bs.get_json(bs.le(f"/{ou}/grades/values/myGradeValues/"), default=[]) or []
    definitions = bs.get_json(bs.le(f"/{ou}/grades/"), default=[]) or []

    defs_by_id = {str(d.get("Id")): d for d in definitions if isinstance(d, dict)}

    rows = []
    for v in values:
        gid = str(v.get("GradeObjectIdentifier") or "")
        d = defs_by_id.get(gid, {})
        num = v.get("PointsNumerator")
        den = v.get("PointsDenominator")
        pct = None
        if isinstance(num, (int, float)) and isinstance(den, (int, float)) and den:
            pct = round(100.0 * num / den, 2)
        rows.append({
            "id": gid,
            "name": v.get("GradeObjectName") or d.get("Name") or "",
            "category": d.get("CategoryName") or (d.get("Category") or {}).get("Name") or "",
            "type": v.get("GradeObjectTypeName") or d.get("GradeType") or "",
            "points_numerator": num,
            "points_denominator": den,
            "percent": pct,
            "weighted_numerator": v.get("WeightedNumerator"),
            "weighted_denominator": v.get("WeightedDenominator"),
            "displayed_grade": v.get("DisplayedGrade"),
            "comments": ((v.get("Comments") or {}).get("Text") or "").strip(),
            "last_modified": v.get("LastModified"),
            "released_date": v.get("ReleasedDate"),
        })

    rows.sort(key=lambda r: (r["category"].lower(), r["name"].lower()))
    payload = {
        "course": {"id": course.id, "name": course.name, "code": course.code},
        "generated_at": now_iso(),
        "grades": rows,
    }
    blob = json.dumps(payload["grades"], sort_keys=True).encode()
    digest = sha256_bytes(blob)

    prev = manifest.data.get("grades_hash")
    changed = prev != digest
    if dry_run:
        return "changed" if changed else "unchanged"

    (course_dir / "grades.json").write_text(json.dumps(payload, indent=2), "utf-8")

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else [
        "id", "name", "category", "type", "points_numerator", "points_denominator",
        "percent", "weighted_numerator", "weighted_denominator", "displayed_grade",
        "comments", "last_modified", "released_date",
    ])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    (course_dir / "grades.csv").write_text(buf.getvalue(), "utf-8")

    manifest.data["grades_hash"] = digest
    manifest.data["grades_synced_at"] = now_iso()
    if changed:
        log.info("  grades updated (%d items)", len(rows))
    else:
        log.info("  grades unchanged (%d items)", len(rows))
    return "changed" if changed else "unchanged"
