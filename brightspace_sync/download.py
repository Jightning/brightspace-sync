"""For file downloads, streamed, atomic, and manifest-aware."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .client import Brightspace
from .limits import DownloadSkipped
from .manifest import Manifest
from .util import human_size, log, sha256_file, slugify

_CD_FILENAME_STAR = re.compile(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", re.I)
_CD_FILENAME = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)', re.I)

@dataclass
class DownloadResult:
    path: Path
    action: str # "downloaded" | "skipped" | "updated" | "dry-run" | "declined" | "error"
    bytes: int = 0
    error: str | None = None

def _name_from_disposition(cd: str | None) -> str | None:
    if not cd:
        return None
    
    m = _CD_FILENAME_STAR.search(cd)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    
    m = _CD_FILENAME.search(cd)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    
    return None

def _safe_name(name: str, fallback: str) -> str:
    """Scrubs the filename so there's nothing weird (like ../../)"""
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    name = name.strip()
    if not name or name in {".", ".."}:
        name = fallback

    stem, dot, ext = name.rpartition(".")
    if dot:
        return slugify(stem, max_len=100) + "." + slugify(ext, max_len=12).replace(" ", "")
    
    return slugify(name, max_len=100)

def download_to(
    bs: Brightspace,
    api_path: str,
    *,
    dest_dir: Path,
    fallback_name: str,
    manifest: Manifest,
    key: str,
    last_modified: str | None,
    force: bool,
    dry_run: bool,
    prefer_name: str | None = None,
) -> DownloadResult:
    """
    Download `api_path` into `dest_dir`.

    Skips the network when the manifest says key is unchanged and local file intact (unless `force`).
    """
    # marks the key seen so --prune doesn't remove it
    manifest.touch(key)

    prev = manifest.get(key)
    if not force and manifest.unchanged(key, last_modified=last_modified):
        return DownloadResult(manifest.abs_path(prev or {}), "skipped")

    if dry_run:
        return DownloadResult(dest_dir / _safe_name(prefer_name or "", fallback_name), "dry-run")

    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    resp = None
    try:
        resp = bs.get_stream(api_path)
        if resp.status_code == 404:
            resp.close()
            return DownloadResult(dest_dir / fallback_name, "error", error="404 not found")
        resp.raise_for_status()
        budget = getattr(bs, 'download_budget', None)
        transfer = budget.transfer(fallback_name, dest_dir) if budget else None
        length = resp.headers.get('Content-Length', '')
        if transfer and length.isdigit():
            transfer.check_size(int(length))

        name = (
            _safe_name(prefer_name, fallback_name)
            if prefer_name
            else _safe_name(_name_from_disposition(resp.headers.get("Content-Disposition")) or "", fallback_name)
        )
        dest = _unique(dest_dir / name, key, manifest)
        tmp = dest.with_name(dest.name + ".part")
        total = 0

        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    if transfer:
                        transfer.consume(len(chunk))
                    fh.write(chunk)
                    total += len(chunk)

        resp.close()
        os.replace(tmp, dest)
        tmp = None
    except DownloadSkipped as exc:
        # Even a forced refresh of an otherwise unchanged file must be retried.
        if prev:
            prev['retry_download'] = True
        _cleanup(tmp)
        log.info("  %s", exc)
        return DownloadResult(manifest.abs_path(prev) if prev else dest_dir / fallback_name,
                              "declined")
    except Exception as exc:
        log.debug("download failed for %s: %s", api_path, exc)
        _cleanup(tmp)
        return DownloadResult(dest_dir / fallback_name, "error", error=str(exc))
    except BaseException: # Ctrl-C / SystemExit: leave no half-written file
        _cleanup(tmp)
        raise
    finally:
        if resp is not None:
            resp.close()

    action = "updated" if prev else "downloaded"
    digest = sha256_file(dest)
    if prev and prev.get("sha256") == digest:
        action = "skipped"  # content identical even though the timestamp moved
    manifest.record(key, last_modified=last_modified, local=dest, sha256=digest)
    log.info("  %-10s %s (%s)", action, dest.name, human_size(total))
    return DownloadResult(dest, action, bytes=total)


def _cleanup(tmp: Path | None) -> None:
    if tmp is not None:
        try:
            tmp.unlink()
        except OSError:
            pass


def _unique(dest: Path, key: str, manifest: Manifest) -> Path:
    """
    Returns a unique version of the destination (no same name dest with a different key)
    """
    prev = manifest.get(key)
    if prev and prev.get("path"): # key already has a destination pre-defined
        p = manifest.abs_path(prev)
        if p.parent == dest.parent:
            return p
        
    if not dest.exists():
        return dest
    
    owned_by_other = any(
        manifest.abs_path(e) == dest and k != key for k, e in manifest.entries.items()
    )

    if not owned_by_other:
        return dest
    
    stem, suffix = dest.stem, dest.suffix
    n = 2

    while (cand := dest.with_name(f"{stem} ({n}){suffix}")).exists():
        n += 1

    return cand
