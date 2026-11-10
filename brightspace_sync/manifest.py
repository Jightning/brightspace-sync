"""
Stored at <course folder>/.bs_manifest.json. 
For every item fetched it keeps the LastModifiedDate + file's path, size and sha256
With this, a re-run only downloads what was last changed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .util import log, now_iso

MANIFEST_NAME = ".bs_manifest.json"

# manifest-key prefix -> which sync section owns it
SECTION_PREFIX = {
    "external:": "content",
    "navigation:": "content",
    "overview:": "content",
    "video:": "videos",
    "transcript:": "transcripts",
    "topic:": "content",
    "moddesc:": "content",    # module description text/html
    "topicdesc:": "content",  # topic description text/html
    "asset:": "content",      # images pulled out of description html
    "news:": "news",
    "dropbox:": "assignments",
}

class Manifest:
    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = path
        self.root = path.parent
        self.data = data
        self.entries: dict[str, dict] = data.setdefault("entries", {})
        self.touched: set[str] = set()

    @classmethod
    def load(cls, course_dir: Path) -> "Manifest":
        path = course_dir / MANIFEST_NAME
        if path.is_file():
            try:
                data = json.loads(path.read_text("utf-8"))
            except (ValueError, OSError) as exc:
                log.warning("manifest %s unreadable (%s), starting fresh", path, exc)
                data = {}
        else:
            data = {}

        return cls(path, data if isinstance(data, dict) else {})

    def save(self) -> None:
        self.data["last_sync"] = now_iso()
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)

    # Helpers ------------------------------------------
    def _rel(self, local: Path) -> str:
        try:
            return os.path.relpath(local, self.root)
        except ValueError:
            return str(local) # different drive (Windows) - keep absolute

    def abs_path(self, entry: dict) -> Path:
        p = Path(entry.get("path", ""))
        return p if p.is_absolute() else (self.root / p)

    # Item Tracking ----------------------------------
    def touch(self, key: str) -> None:
        self.touched.add(key)

    def touch_prefix(self, prefix: str) -> None:
        """Keep everything filed under `prefix` alive through --prune. Used when
        a parent item was skipped as unchanged, so the children it owns (a
        description's images, say) were never re-fetched and so never touched."""
        self.touched.update(k for k in self.entries if k.startswith(prefix))

    def unchanged(self, key: str, *, last_modified: str | None) -> bool:
        ent = self.entries.get(key)
        if not ent or ent.get('retry_download'):
            return False
        local = self.abs_path(ent)
        if not local.exists():
            return False
        if ent.get("last_modified") != (last_modified or ""):
            return False
        try:
            if ent.get("size") is not None and local.stat().st_size != ent["size"]:
                return False
        except OSError:
            return False
        return True

    def record(self, key: str, *, last_modified: str | None, local: Path,
               sha256: str | None = None, extra: dict | None = None) -> None:
        entry: dict[str, Any] = {
            "last_modified": last_modified or "",
            "path": self._rel(local),
            "synced_at": now_iso(),
        }

        try:
            entry["size"] = local.stat().st_size
        except OSError:
            entry["size"] = None
        if sha256:
            entry["sha256"] = sha256
        if extra:
            entry.update(extra)

        self.entries[key] = entry
        self.touched.add(key)

    def get(self, key: str) -> dict | None:
        """Gets value from entries"""
        return self.entries.get(key)

    # Prune ----------------------------------------------
    def prune(self, sections_run: set[str], *, dry_run: bool) -> list[Path]:
        """
        Drops entries (and their files) that belong to a synced section but Brightspace no longer serves.
        Returns removed paths.
        """
        removed: list[Path] = []
        for key in list(self.entries):
            if key in self.touched:
                continue

            section = next((s for p, s in SECTION_PREFIX.items() if key.startswith(p)), None)
            if section is None or section not in sections_run:
                continue

            entries = self.entries[key]
            if not entries.get("path"):          # bogus entry from a failed download
                self.entries.pop(key, None)
                continue

            local = self.abs_path(entries)
            if local.is_dir() or not local.exists():
                self.entries.pop(key, None)
                continue
            removed.append(local)

            if not dry_run:
                try:
                    local.unlink()
                except OSError:
                    pass
                self.entries.pop(key, None)
                
        return removed
