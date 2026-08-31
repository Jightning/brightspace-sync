"""Plain-data model behind the setup screen.

Keeps all the config<->form translation (including the rename / merge rules) out
of the tkinter code so it can be unit-tested without a display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from ..config import (Config, CourseRule, MapEntry, _dir_str, app_dir)
from ..enrollments import Course
from ..util import slugify

SECTIONS = ("content", "grades", "news", "assignments", "calendar", "videos")


@dataclass
class CourseChoice:
    id: int
    code: str
    name: str
    default_folder: str
    selected: bool = True
    folder: str = ""      # blank -> use default_folder
    merge_key: str = ""   # blank -> not merged
    subfolder: str = ""   # blank -> derive from code / name

    @classmethod
    def from_course(cls, c: Course) -> "CourseChoice":
        return cls(id=c.id, code=c.code or "", name=c.name or "",
                   default_folder=c.folder)


@dataclass
class MergeGroup:
    key: str
    folder: str = ""


@dataclass
class SetupModel:
    base_url: str = "https://purdue.brightspace.com"
    browser: str = "chrome"
    chrome_profile: str = ""
    data_dir: str = "bs_data"
    include: dict = field(default_factory=lambda: {
        "content": True, "grades": True, "news": True,
        "assignments": True, "calendar": False, "videos": False})
    sync_all: bool = True                       # True -> courses.mode: all
    courses: List[CourseChoice] = field(default_factory=list)
    merges: List[MergeGroup] = field(default_factory=list)

    # advanced tuning
    request_delay_seconds: float = 0.4
    max_retries: int = 4
    prune: bool = False
    video_quality: str = "1080"                 # "best" or an int as a string
    video_rate_limit: str = ""
    video_timeout: int = 0

    # non-id ignore rules from a hand-edited file (e.g. match: "Sandbox") - kept as-is
    extra_ignore: List[CourseRule] = field(default_factory=list)

    # --- reading ----------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Config, all_courses: List[Course]) -> "SetupModel":
        m = cls()
        m.base_url = cfg.base_url
        m.browser = cfg.browser
        m.chrome_profile = cfg.chrome_profile or ""
        m.data_dir = _dir_str(cfg.data_dir)
        m.include = {k: cfg.includes_section(k) for k in
                     ("content", "grades", "news", "assignments", "calendar", "videos")}
        m.request_delay_seconds = cfg.request_delay_seconds
        m.max_retries = cfg.max_retries
        m.prune = cfg.prune
        m.video_quality = "best" if not cfg.video_quality else str(cfg.video_quality)
        m.video_rate_limit = cfg.video_rate_limit
        m.video_timeout = cfg.video_timeout
        m.sync_all = cfg.courses_mode == "all"
        m.extra_ignore = [r for r in cfg.ignore if r.match and r.id is None]

        m.courses = [CourseChoice.from_course(c) for c in all_courses]
        by_id = {cc.id: cc for cc in m.courses}
        for c in all_courses:
            by_id[c.id].selected = cfg.wants_course(ou_id=c.id, name=c.name, code=c.code)

        for gi, entry in enumerate(cfg.mapping):
            members = [c for c in all_courses
                       if entry.claims(ou_id=c.id, name=c.name, code=c.code)]
            if not members:
                continue
            if len(members) > 1:
                key = f"g{gi}"
                m.merges.append(MergeGroup(key=key, folder=entry.name))
                for c in members:
                    cc = by_id[c.id]
                    cc.merge_key = key
                    cc.subfolder = entry.subfolder_for(
                        ou_id=c.id, name=c.name, code=c.code) or ""
            else:
                by_id[members[0].id].folder = entry.name
        return m

    # --- writing --------------------------------------------------------
    def to_config(self) -> Config:
        cfg = Config()
        cfg.base_url = self.base_url.rstrip("/")
        cfg.browser = self.browser
        cfg.chrome_profile = self.chrome_profile.strip() or None

        dd = Path(self.data_dir.strip() or "bs_data").expanduser()
        cfg.data_dir = dd if dd.is_absolute() else (app_dir() / dd)
        cfg.courses_dir = cfg.data_dir / "courses"
        cfg.log_dir = cfg.data_dir / "logs"
        cfg.cookies_txt_path = cfg.data_dir / "cookies.txt"

        cfg.include = {k: bool(self.include.get(k, False)) for k in
                       ("content", "grades", "news", "assignments", "calendar", "videos")}
        cfg.request_delay_seconds = float(self.request_delay_seconds)
        cfg.max_retries = int(self.max_retries)
        cfg.prune = bool(self.prune)
        vq = str(self.video_quality).strip().lower()
        cfg.video_quality = None if vq in ("", "0", "best", "none") else int(vq)
        cfg.video_rate_limit = self.video_rate_limit.strip()
        cfg.video_timeout = int(self.video_timeout or 0)

        selected = [c for c in self.courses if c.selected]
        cfg.courses_mode = "all" if self.sync_all else "allowlist"

        allow: List[CourseRule] = []
        ignore: List[CourseRule] = list(self.extra_ignore)
        mapping: List[MapEntry] = []

        if self.sync_all:
            for c in self.courses:
                if not c.selected:
                    ignore.append(CourseRule(id=c.id))
        else:
            allow = [CourseRule(id=c.id) for c in selected]

        for c in selected:                       # renames
            if c.merge_key:
                continue
            name = c.folder.strip()
            if name and slugify(name) != c.default_folder:
                mapping.append(MapEntry(name=name, selectors=[CourseRule(id=c.id)]))

        for g in self.merges:                     # merges
            members = [c for c in selected if c.merge_key == g.key]
            if len(members) < 2:
                continue
            folder = g.folder.strip() or members[0].default_folder
            subs = [(CourseRule(match=(c.code or str(c.id))),
                     (c.subfolder.strip() or c.code or c.name or str(c.id)))
                    for c in members]
            mapping.append(MapEntry(
                name=folder,
                selectors=[CourseRule(id=c.id) for c in members],
                subfolders=subs))

        cfg.allow, cfg.ignore, cfg.mapping = allow, ignore, mapping
        return cfg

    # --- checks -------------------------------------------------------
    def problems(self) -> List[str]:
        out: List[str] = []
        if not self.base_url.startswith("https://"):
            out.append("The Brightspace address must start with https://")
        if not self.data_dir.strip():
            out.append("Choose a folder for the downloaded files.")
        if not self.sync_all and not any(c.selected for c in self.courses):
            out.append("Pick at least one course, or turn on “All my courses”.")
        for g in self.merges:
            members = [c for c in self.courses if c.selected and c.merge_key == g.key]
            if len(members) == 1:
                out.append(f"Merge group “{g.folder or g.key}” only has one "
                           f"course selected - it will be treated as a rename.")
        return out
