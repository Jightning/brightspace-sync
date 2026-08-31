from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml

from .util import log


def app_dir() -> Path:
    """Folder the program lives in: next to the frozen executable, else the cwd.

    The downloaded (PyInstaller) build keeps its config and data here so a
    non-technical user can just drop the file in a folder and double-click it.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def config_search_paths() -> tuple[Path, ...]:
    return (
        Path.cwd() / "config.yaml",
        Path.cwd() / "bs_data" / "config.yaml",
        app_dir() / "config.yaml",
        app_dir() / "bs_data" / "config.yaml",
    )


def default_config_path() -> Path:
    """Where the GUI writes config.yaml when there isn't one yet."""
    return app_dir() / "config.yaml"

_VALID_BROWSERS = {"auto", "chrome", "edge", "brave", "firefox", "safari",
                   "cookies_txt", "webview"}

# Known keys in config
_KNOWN_TOP = {
    "base_url", "browser", "chrome_profile", "data_dir", "courses_dir",
    "cookies_txt_path", "request_delay_seconds", "max_retries", "prune",
    "video_rate_limit", "video_quality", "video_timeout",
    "include", "courses",
}
_KNOWN_COURSES = {"mode", "allow", "ignore", "map"}


@dataclass
class CourseRule:
    """A course rule defined by the user"""
    id: int | None = None # course code
    match: str | None = None # string to use to match with course (match if it's a substring of the course name or id)

    def matches(self, *, ou_id: int, name: str, code: str) -> bool:
        if self.id is not None:
            return int(self.id) == int(ou_id)
        if self.match:
            needle = self.match.casefold()
            return needle in (name or "").casefold() or needle in (code or "").casefold()
        return False

@dataclass
class MapEntry:
    """A course to folder mapping"""
    name: str
    selectors: list[CourseRule] = field(default_factory=list)
    subfolders: list[tuple[CourseRule, str]] = field(default_factory=list)  # (rule, label)

    def claims(self, *, ou_id: int, name: str, code: str) -> bool:
        return any(r.matches(ou_id=ou_id, name=name, code=code) for r in self.selectors)

    def subfolder_for(self, *, ou_id: int, name: str, code: str) -> str | None:
        for rule, label in self.subfolders:
            if rule.matches(ou_id=ou_id, name=name, code=code):
                return label
        return None

@dataclass
class Config:
    # Default configs
    base_url: str = "https://purdue.brightspace.com" # Brightspace URL to connect to
    browser: str = "auto" # cookie source (auto tries chrome, edge, brave, firefox, safari)
    chrome_profile: str | None = None # Chrome/Edge/Brave profile folder, e.g. "Default" or "Profile 1"
    data_dir: Path = field(default_factory=lambda: app_dir() / "bs_data") # ONE local folder that holds everything the tool writes
    cookies_txt_path: Path = field(default_factory=lambda: app_dir() / "bs_data" / "cookies.txt") # manual cookies file (browser: cookies_txt)
    courses_dir: Path = field(default_factory=lambda: app_dir() / "bs_data" / "courses") # where course content is saved
    log_dir: Path = field(default_factory=lambda: app_dir() / "bs_data" / "logs") # where the run log is written
    request_delay_seconds: float = 0.4 # rate limiting
    max_retries: int = 4 # number retries for http failures
    prune: bool = False # delete local files that no longer exist in Brightspace
    video_rate_limit: str = "" # throttle video downloads, empty means full speed
    video_quality: int | None = 1080 # capped video resolution, None/"best" means best available
    video_timeout: int = 7200 # seconds per video before giving up, 0 means no limit
    include: dict[str, bool] = field(default_factory=dict) # enabled content sections
    courses_mode: str = "allowlist" # 'allowlist' | 'all'
    allow: list[CourseRule] = field(default_factory=list) # courses included
    ignore: list[CourseRule] = field(default_factory=list) # courses excluded
    mapping: list[MapEntry] = field(default_factory=list) # folder-name / merge rules
    source_path: Path | None = None # config file path that was loaded

    # course matching
    def wants_course(self, *, ou_id: int, name: str, code: str) -> bool:
        if any(r.matches(ou_id=ou_id, name=name, code=code) for r in self.ignore):
            return False
        if self.courses_mode == "all":
            return True
        if any(r.matches(ou_id=ou_id, name=name, code=code) for r in self.allow):
            return True
        # naming a course in `map` implies you want it synced
        return any(m.claims(ou_id=ou_id, name=name, code=code) for m in self.mapping)

    def includes_section(self, key: str) -> bool:
        return bool(self.include.get(key, False))


def _rules(raw: list[Any] | None) -> list[CourseRule]:
    """Parse an allow/ignore list [{id/match} | int | str]."""
    out: list[CourseRule] = []

    for item in raw or []:
        if isinstance(item, dict):
            out.append(CourseRule(id=item.get("id"), match=item.get("match")))
        elif isinstance(item, bool):
            continue
        elif isinstance(item, int):
            out.append(CourseRule(id=item))
        elif isinstance(item, str):
            out.append(CourseRule(match=item))
    return out

def _selectors(item: dict) -> list[CourseRule]:
    """`match`/`id` in a map entry: each may be a scalar or a list."""
    out: list[CourseRule] = []

    m = item.get("match")
    for v in (m if isinstance(m, list) else [m] if m else []):
        out.append(CourseRule(match=str(v)))

    i = item.get("id")
    for v in (i if isinstance(i, list) else [i] if i is not None else []):
        out.append(CourseRule(id=int(v)))

    return out


def _map_entries(raw: Any) -> list[MapEntry]:
    out: list[MapEntry] = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("name"):
            log.warning("courses.map: skipping entry without a `name`: %r", item)
            continue
        subs: list[tuple[CourseRule, str]] = []
        for key, label in (item.get("subfolders") or {}).items():
            subs.append((CourseRule(match=str(key)), str(label)))
        out.append(MapEntry(name=str(item["name"]),
                            selectors=_selectors(item),
                            subfolders=subs))
    return out


def find_config(path_str: str | None) -> Path:
    if path_str:
        p = Path(path_str).expanduser().resolve()

        if not p.is_file():
            raise SystemExit(f"config file not found: {p}")
        
        return p
    
    for candidate in config_search_paths():
        if candidate.is_file():
            return candidate.resolve()

    raise SystemExit("No config.yaml found. Copy config.example.yaml to ./config.yaml and edit.")


def _resolve_under(value: str, data_dir: Path, *, label: str) -> Path:
    """
    Relative path is relative to data_dir.
    Absolute or ~ path is flagged.
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        try:
            path.relative_to(data_dir)
        except ValueError:
            log.warning("%s (%s) is outside data_dir (%s).", label, path, data_dir)
        return path
    
    return (data_dir / path).resolve()


def load_config(path_str: str | None = None) -> Config:
    path = find_config(path_str)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: top level must be a mapping")

    unknown = sorted(set(raw) - _KNOWN_TOP)
    if unknown:
        log.warning("%s: unrecognized key(s) %s - looks like an older template. "
                    "Regenerate it from config.example.yaml.", path.name, unknown)

    # Loading all the configs ------
    cfg = Config()
    cfg.source_path = path
    cfg.base_url = str(raw.get("base_url", cfg.base_url)).rstrip("/")
    if not cfg.base_url.startswith("https://"):
        raise SystemExit(f"{path}: base_url must be https://")

    cfg.browser = str(raw.get("browser", cfg.browser)).lower()
    if cfg.browser not in _VALID_BROWSERS:
        raise SystemExit(f"{path}: browser must be one of {sorted(_VALID_BROWSERS)}")
    cp = raw.get("chrome_profile")
    cfg.chrome_profile = str(cp) if cp else None

    dd = Path(str(raw.get("data_dir", "bs_data"))).expanduser()
    if not dd.is_absolute():
        dd = path.parent / dd
    cfg.data_dir = dd.resolve()
    cfg.log_dir = cfg.data_dir / "logs"
    cfg.cookies_txt_path = (
        _resolve_under(str(raw["cookies_txt_path"]), cfg.data_dir, label="cookies_txt_path")
        if "cookies_txt_path" in raw else cfg.data_dir / "cookies.txt"
    )
    cfg.courses_dir = (
        _resolve_under(str(raw["courses_dir"]), cfg.data_dir, label="courses_dir")
        if "courses_dir" in raw else cfg.data_dir / "courses"
    )

    cfg.request_delay_seconds = float(raw.get("request_delay_seconds", cfg.request_delay_seconds))
    cfg.max_retries = int(raw.get("max_retries", cfg.max_retries))
    cfg.prune = bool(raw.get("prune", cfg.prune))

    cfg.video_rate_limit = str(raw.get("video_rate_limit", "") or "")
    vq = raw.get("video_quality", 1080)
    cfg.video_quality = None if str(vq).strip().lower() in ("", "0", "best", "none") else int(vq)
    cfg.video_timeout = int(raw.get("video_timeout", cfg.video_timeout) or 0)

    inc = raw.get("include") or {}
    cfg.include = {
        "content": bool(inc.get("content", True)),
        "grades": bool(inc.get("grades", True)),
        "news": bool(inc.get("news", True)),
        "assignments": bool(inc.get("assignments", True)),
        "calendar": bool(inc.get("calendar", False)),
        "videos": bool(inc.get("videos", False)),
    }

    courses: dict[Any, list[Any]] | None = raw.get("courses") or {}
    unknown_c = sorted(set(courses) - _KNOWN_COURSES)
    if unknown_c:
        log.warning("%s: courses.%s not recognized", path.name, unknown_c)
    cfg.courses_mode = str(courses.get("mode", cfg.courses_mode)).lower()
    if cfg.courses_mode not in {"allowlist", "all"}:
        raise SystemExit(f"{path}: courses.mode must be 'allowlist' or 'all'")
    cfg.allow = _rules(courses.get("allow"))
    cfg.ignore = _rules(courses.get("ignore"))
    cfg.mapping = _map_entries(courses.get("map"))

    if cfg.courses_mode == "allowlist" and not cfg.allow and not cfg.mapping:
        log.warning("courses.mode is 'allowlist' but nothing is listed under "
                    "courses.allow / courses.map - nothing will sync. "
                    "Run `brightspace_sync list`.")

    return cfg

# Writing config back out (used by the GUI) --------------------------------

_SAVED_HEADER = (
    "# Written by the Brightspace Sync app - safe to edit by hand.\n"
    "# Every option is documented in config.example.yaml\n\n"
)

def _dir_str(path: Path) -> str:
    """Keep the folder relative to the app when it sits under it, else absolute."""
    try:
        rel = path.resolve().relative_to(app_dir())
    except ValueError:
        return str(path)
    return str(rel) if str(rel) not in (".", "") else "bs_data"


def _rule_to_yaml(r: CourseRule) -> dict:
    return {"id": int(r.id)} if r.id is not None else {"match": r.match}


def _map_entry_to_yaml(e: MapEntry) -> dict:
    out: dict[str, Any] = {"name": e.name}
    matches = [r.match for r in e.selectors if r.match]
    ids = [int(r.id) for r in e.selectors if r.id is not None]
    if matches:
        out["match"] = matches if len(matches) > 1 else matches[0]
    if ids:
        out["id"] = ids if len(ids) > 1 else ids[0]
    if e.subfolders:
        out["subfolders"] = {rule.match: label for rule, label in e.subfolders}
    return out


def config_to_yaml_dict(cfg: Config) -> dict:
    data: dict[str, Any] = {"base_url": cfg.base_url, "browser": cfg.browser}
    if cfg.chrome_profile:
        data["chrome_profile"] = cfg.chrome_profile
    data["data_dir"] = _dir_str(cfg.data_dir)
    data["include"] = {k: bool(v) for k, v in cfg.include.items()}
    data["courses"] = {
        "mode": cfg.courses_mode,
        "allow": [_rule_to_yaml(r) for r in cfg.allow],
        "ignore": [_rule_to_yaml(r) for r in cfg.ignore],
        "map": [_map_entry_to_yaml(e) for e in cfg.mapping],
    }
    data["request_delay_seconds"] = cfg.request_delay_seconds
    data["max_retries"] = cfg.max_retries
    data["prune"] = cfg.prune
    data["video_quality"] = cfg.video_quality if cfg.video_quality else "best"
    data["video_rate_limit"] = cfg.video_rate_limit
    data["video_timeout"] = cfg.video_timeout
    return data


def save_config(cfg: Config, path: str | Path) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(config_to_yaml_dict(cfg), sort_keys=False, allow_unicode=True)
    path.write_text(_SAVED_HEADER + body, "utf-8")
    return path
