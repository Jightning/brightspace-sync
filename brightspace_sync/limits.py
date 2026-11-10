"""Shared download limits and explicit, run-scoped approval to exceed them."""

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Callable

from .util import human_size

MIB = 1024 ** 2


class DownloadStopped(BaseException):
    """Cancellation must escape optional-download exception/fallback handlers."""


class DownloadSkipped(BaseException):
    """Skip one transfer without triggering downloader retries or fallbacks."""


@dataclass(frozen=True)
class LimitRequest:
    kind: str
    label: str
    limit: int
    requested: int

    @property
    def message(self) -> str:
        scope = "this file" if self.kind == "file" else "this run"
        rejection = ("No skips this file; you will be asked again next sync."
                     if self.kind == "file" else "No stops the sync and keeps completed files.")
        return (f"Downloading {self.label} would exceed the {self.kind} limit "
                f"({human_size(self.limit)}; at least {human_size(self.requested)} needed).\n\n"
                f"Allow exceeding this limit for {scope}? Other limits still apply. "
                f"{rejection} Approval is not saved.")


class DownloadBudget:
    def __init__(self, root: Path, file_limit: int, run_limit: int, storage_limit: int,
                 confirm: Callable[[LimitRequest], bool] | None = None,
                 should_stop: Callable[[], bool] | None = None):
        if min(file_limit, run_limit, storage_limit) <= 0:
            raise ValueError("download limits must be positive")
        self.file_limit, self.run_limit, self.storage_limit = file_limit, run_limit, storage_limit
        self.confirm = confirm
        self.should_stop = should_stop or (lambda: False)
        self.used = 0
        self.root = root
        self.stored = sum(p.stat().st_size for p in root.rglob('*')
                          if p.is_file() and not p.is_symlink()) if root.exists() else 0
        self.approved = set()

    def transfer(self, label: str, directory: Path | None = None):
        return Transfer(self, label, directory)

    def check(self, kind: str, label: str, value: int, limit: int, approved: set):
        if self.should_stop():
            raise DownloadStopped("Sync stopped by user; completed files were kept.")
        if value <= limit or kind in approved:
            return
        request = LimitRequest(kind, label, limit, value)
        try:
            decision = self.confirm is not None and self.confirm(request)
        except Exception:
            raise DownloadStopped('Could not obtain download-limit approval; sync stopped. Re-run interactively.') from None
        if self.should_stop():
            raise DownloadStopped("Sync stopped by user; completed files were kept.")
        if not decision:
            if kind == 'file':
                raise DownloadSkipped(f"Skipped {label}: file limit declined; will ask again next sync.")
            raise DownloadStopped(request.message + "\nSync stopped; no further downloads were authorized. "
                                  "Re-run interactively to approve, or adjust download_limits in config.yaml.")
        approved.add(kind)


class Transfer:
    def __init__(self, budget: DownloadBudget, label: str, directory: Path | None = None):
        self.budget, self.label = budget, label
        self.directory = directory or budget.root
        self.size = 0
        self.approved = set()

    def check_size(self, size: int):
        budget = self.budget
        remaining = max(0, size - self.size)
        budget.check('file', self.label, size, budget.file_limit, self.approved)
        budget.check('run', self.label, budget.used + remaining, budget.run_limit, budget.approved)
        # Count temporary/replacement files conservatively, without assuming a
        # successful replacement or deleting older material to make room.
        budget.check('storage', self.label, budget.stored + budget.used + remaining,
                     budget.storage_limit, budget.approved)
        directory = self.directory
        while not directory.exists():
            directory = directory.parent
        if shutil.disk_usage(directory).free < remaining + 64 * MIB:
            raise DownloadStopped('Insufficient free disk space; sync stopped to preserve 64 MiB free. '
                                  'Free space and re-run; completed files were kept.')

    def consume(self, amount: int):
        self.check_size(self.size + amount)
        self.size += amount
        self.budget.used += amount


def progress_hook(budget: DownloadBudget | None):
    """Apply the same prompts to yt-dlp media, captions and temporary audio."""
    transfers = {}

    def hook(data):
        if budget is None or data.get('status') not in ('downloading', 'finished'):
            return
        name = str(data.get('filename') or data.get('tmpfilename') or 'media')
        downloaded = int(data.get('downloaded_bytes') or 0)
        transfer = transfers.get(name)
        if transfer is None or downloaded < transfer.size:
            transfer = transfers[name] = budget.transfer(Path(name).name, Path(name).parent)
        total = data.get('total_bytes') or data.get('total_bytes_estimate')
        if total:
            transfer.check_size(int(total))
        transfer.consume(max(0, downloaded - transfer.size))

    return hook


def youtube_dl_class(module, budget):
    """Do not delegate capped network transfers to an unmetered subprocess."""
    if budget is None:
        return module.YoutubeDL
    from yt_dlp.downloader import get_suitable_downloader
    from yt_dlp.downloader.external import ExternalFD

    class CappedYoutubeDL(module.YoutubeDL):
        def dl(self, name, info, subtitle=False, test=False):
            from urllib.parse import urlsplit
            protocol = info.get('protocol') or urlsplit(info.get('url', '')).scheme
            if protocol not in ('http', 'https', 'm3u8', 'm3u8_native', 'http_dash_segments',
                                'http_dash_segments_generator', 'dash_frag_urls', 'm3u8_frag_urls'):
                raise DownloadStopped('Unsupported media protocol for capped downloads; sync stopped. '
                                      'Use the saved online link instead.')
            downloader = get_suitable_downloader(info, self.params)
            if downloader and issubclass(downloader, ExternalFD):
                raise DownloadStopped('This media protocol requires an external downloader that cannot '
                                      'honor interactive limits. Sync stopped; use its saved online link.')
            return super().dl(name, info, subtitle=subtitle, test=test)

    return CappedYoutubeDL


def postprocess_hook(budget):
    def hook(data):
        if budget is None or data.get('status') != 'started':
            return
        info = data.get('info_dict') or {}
        files = info.get('__files_to_merge') or []
        if not files:
            return
        size = sum(Path(path).stat().st_size for path in files)
        label = Path(info.get('filepath') or 'merged video').name
        budget.check('file', label, size, budget.file_limit, set())
        budget.check('storage', label, budget.stored + budget.used + size,
                     budget.storage_limit, budget.approved)
        directory = Path(files[0]).parent
        if shutil.disk_usage(directory).free < size + 64 * MIB:
            raise DownloadStopped('Insufficient free disk space for video merge; sync stopped.')
    return hook
