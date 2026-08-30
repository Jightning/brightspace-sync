"""
Decides which folder the course goes to (based on the mapping in config)

Rules:
  * No mapping for a course -> it uses its auto name (Course.folder).
  * Only one map entry for a course -> <courses_dir>/<entry.name>/ .
  * Multiple map entries for a course ("merge") -> they share <courses_dir>/<entry.name>/ and each gets its own subfolder
  * If two map entries claim a course, the first one wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .enrollments import Course
from .util import slugify


@dataclass
class Placement:
    folder: str # top-level folder under courses_dir
    subfolder: str | None # per-course subfolder when merged, else None
    merged_with: int # how many courses share `folder` (1 means not merged)

    def parts(self) -> list[str]:
        return [self.folder] + ([self.subfolder] if self.subfolder else [])


def resolve_placements(cfg: Config, courses: list[Course]) -> dict[int, Placement]:
    # entry index -> the selected courses it claims
    claimed: dict[int, tuple[int, "object"]] = {}
    entry_hits: dict[int, list[Course]] = {}
    for idx, entry in enumerate(cfg.mapping):
        hits = [c for c in courses
                if entry.claims(ou_id=c.id, name=c.name, code=c.code)]
        entry_hits[idx] = hits
        for c in hits:
            claimed.setdefault(c.id, (idx, entry))

    out: dict[int, Placement] = {}
    for c in courses:
        if c.id not in claimed:
            out[c.id] = Placement(folder=c.folder, subfolder=None, merged_with=1)
            continue
        idx, entry = claimed[c.id]
        siblings = entry_hits[idx]
        folder = slugify(entry.name) # type: ignore
        if len(siblings) <= 1:
            out[c.id] = Placement(folder=folder, subfolder=None, merged_with=1)
        else:
            label = entry.subfolder_for(ou_id=c.id, name=c.name, code=c.code) or c.code or c.name or str(c.id) # type: ignore
            out[c.id] = Placement(folder=folder, subfolder=slugify(label),
                                  merged_with=len(siblings))
    return out
