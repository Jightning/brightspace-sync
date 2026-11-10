"""
Reading from the Brightspace API.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import requests

from .config import Config
from .util import log

class BrightspaceError(RuntimeError):
    pass

class AuthExpired(BrightspaceError):
    """Raised if the session expires mid run"""

class Brightspace:
    def __init__(self, session: requests.Session, cfg: Config):
        self.session = session # authenticated session
        self.base = cfg.base_url # brightspace url
        self._ver_cache: dict[str, str] = {} # API version
        self.max_retries = max(0, cfg.max_retries) # max retries on 429/5xx
        self.delay = max(0.0, cfg.request_delay_seconds) # delay in seconds
        self._last_call = 0.0 # timing for rate limiting

    # Version handling -----------------
    def version(self, product: str) -> str:
        """Finds the latest supported API version, capped at 1.x for stable responses. Falls back to '1.0'."""
        if product in self._ver_cache:
            return self._ver_cache[product]
        ver = "1.0"

        try:
            response = self._raw_get(f"/d2l/api/{product}/versions/")
            response.raise_for_status()
            data = response.json()
            supported = data.get("SupportedVersions", [])
            ones = sorted(
                (v for v in supported if isinstance(v, str) and v.startswith("1.")
                 and v[2:].isdigit()),
                key=lambda v: [int(p) for p in v.split(".") if p.isdigit()],
            )
            if ones:
                ver = ones[-1]
        except Exception as exc:  # noqa: BLE001 - versions endpoint is optional
            log.debug("version discovery for %s failed, using 1.0 (%s)", product, exc)

        self._ver_cache[product] = ver
        log.debug("using %s API version %s", product, ver)
        return ver

    def le(self, path: str) -> str:
        """Learning Environment (LE) API path"""
        return f"/d2l/api/le/{self.version('le')}{path}"

    def le_unstable(self, path: str) -> str:
        """
        Learning Environment 'unstable' route 
        This is the only place the new content experience exposes
        module/topic Description HTML (the text shown above/below topics).
        """
        return f"/d2l/api/le/unstable{path}"

    def lp(self, path: str) -> str:
        """Learning Platform (LP) API path"""
        return f"/d2l/api/lp/{self.version('lp')}{path}"

    # Low Level stuff ---------------------------------------------
    def _throttle(self) -> None:
        """Waits until the delay from the last request is over"""
        if self.delay:
            wait = self.delay - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)

        self._last_call = time.monotonic()

    def _raw_get(self, path: str, *, params=None, stream=False,
                 allow_forbidden=False) -> requests.Response:
        """HTTP request to path"""
        url = path if path.startswith("http") else f"{self.base}{path}"
        attempt = 0

        while True: # loop for retries (with an exponential backoff timer + throttle between)
            self._throttle()

            try:
                response = self.session.get(url, params=params, stream=stream, timeout=60)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise BrightspaceError(f"GET {url} failed: {exc}") from exc

                backoff = 2 ** attempt
                log.warning("network error on %s, retry in %ss (%s)", path, backoff, exc)
                time.sleep(backoff)
                attempt += 1
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= self.max_retries:
                    response.raise_for_status()

                retry_after = response.headers.get("Retry-After")
                backoff = int(retry_after) if (retry_after or "").isdigit() else 2 ** attempt # type: ignore
                log.warning("HTTP %d on %s, retry in %ss", response.status_code, path, backoff)
                response.close()
                time.sleep(backoff)
                attempt += 1
                continue

            if response.status_code == 401 or (response.status_code == 403 and not allow_forbidden):
                raise AuthExpired(
                    f"HTTP {response.status_code} on {path}.\n"
                    f"The browser session stopped being accepted. Re-open Brightspace to refresh it, then run again."
                )
            
            return response

    # Helpers -------------------------------------
    def get_json(self, path: str, *, params=None, default: Any = None) -> Any:
        """Makes an http request to path and parses the resulting json"""
        response = self._raw_get(path, params=params)

        if response.status_code == 404:
            log.debug("404 %s (treating as empty)", path)
            return default
        response.raise_for_status()

        if not response.content:
            return default

        try:
            return response.json()
        except ValueError as exc:
            raise BrightspaceError(f"non-JSON response from {path}: {exc}") from exc

    def get_stream(self, path: str, *, params=None) -> requests.Response:
        """Streams http request from path, for downloading files."""
        response = self._raw_get(path, params=params, stream=True)
        return response

    def paged(self, path: str, *, params=None) -> Iterator[dict]:
        """Iterate bookmark pages, ObjectListPage Next URLs, or legacy arrays."""
        params = dict(params or {})
        seen_bookmarks: set[str] = set()
        seen_urls: set[str] = set()

        while True:
            page = self.get_json(path, params=params, default={}) or {}
            if isinstance(page, list):
                yield from page
                return
            if not isinstance(page, dict):
                raise BrightspaceError(f"invalid paged response from {path}")
            items = page.get("Items", page.get("Objects", []))
            for it in items:
                yield it

            next_url = page.get("Next")
            if next_url:
                if not isinstance(next_url, str) or next_url in seen_urls:
                    raise BrightspaceError(f"invalid or repeated Next URL from {path}")
                seen_urls.add(next_url)
                path = next_url
                # Next already contains the original filters and paging state.
                params = {}
                continue

            info = page.get("PagingInfo") or {}
            if not info.get("HasMoreItems"):
                return

            bookmark = str(info.get("Bookmark") or "")
            if not bookmark or bookmark in seen_bookmarks:
                return

            seen_bookmarks.add(bookmark)
            params["bookmark"] = bookmark
