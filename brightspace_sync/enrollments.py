"""List the user's course enrollments and apply the config allow/ignore filter."""

from __future__ import annotations

from dataclasses import dataclass

from .client import Brightspace
from .config import Config
from .util import log, slugify

# OrgUnit type 3 = Course Offering (where students are enrolled).
COURSE_OFFERING_TYPE_ID = 3

@dataclass
class Course:
    id: int
    name: str
    code: str
    is_active: bool
    can_access: bool

    @property
    def folder(self) -> str:
        """Folder name for a course"""
        base = self.code.strip() or self.name.strip() or str(self.id)
        if self.code and self.name and self.code.strip() not in self.name:
            base = f"{self.code.strip()} - {self.name.strip()}"
        return slugify(base)

def list_all_courses(bs: Brightspace) -> list[Course]:
    path = bs.lp("/enrollments/myenrollments/")
    params = {"orgUnitTypeId": str(COURSE_OFFERING_TYPE_ID), "sortBy": "OrgUnitName"}
    out: list[Course] = []
    seen: set[int] = set()

    for item in bs.paged(path, params=params):
        ou = item.get("OrgUnit") or {}
        type_id = ((ou.get("Type") or {}).get("Id"))
        if type_id != COURSE_OFFERING_TYPE_ID:
            continue

        ou_id = int(ou.get("Id")) # type: ignore
        if ou_id in seen:
            continue

        seen.add(ou_id)
        access = item.get("Access") or {}
        out.append(
            Course(
                id=ou_id,
                name=ou.get("Name") or "",
                code=ou.get("Code") or "",
                is_active=bool(access.get("IsActive", True)),
                can_access=bool(access.get("CanAccess", True)),
            )
        )
        
    out.sort(key=lambda c: (not c.is_active, c.name.lower()))
    return out

def select_courses(
    cfg: Config,
    all_courses: list[Course],
    cli_ids: list[int] | None = None,
) -> list[Course]:
    """
    Resolve which courses to actually sync.

    * --course ids on the CLI always wins
    * otherwise apply config allow/ignore rules
    """
    if cli_ids:
        wanted = set(cli_ids)
        chosen = [c for c in all_courses if c.id in wanted]
        missing = wanted - {c.id for c in chosen}
        for m in missing:
            log.warning("--course %s: not found in your enrollments, skipping", m)
    else:
        chosen = [
            c for c in all_courses
            if cfg.wants_course(ou_id=c.id, name=c.name, code=c.code)
        ]

    usable = []
    for c in chosen:
        if not c.can_access:
            log.warning("Skipping %s (%s): you no longer have access", c.code or c.id, c.name)
            continue
        usable.append(c)

    return usable
