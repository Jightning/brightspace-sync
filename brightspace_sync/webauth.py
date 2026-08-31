"""
Sign-in fallback that opens a login window and reads the cookies from there
"""

from __future__ import annotations

import http.cookiejar as cookiejar
import json
import subprocess
import sys
import threading
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .config import Config
from .util import log

SUBCOMMAND = "_webview-login"

_SESSION_COOKIE = "d2lSessionVal"
_SECURE_COOKIE = "d2lSecureSessionVal"

_WINDOW_TIMEOUT = 300.0        # seconds the user has to finish signing in
_SUBPROCESS_TIMEOUT = 360.0    # a bit more, to let the child shut its window down

# Marker kept in the failure text so the caller can tell "user closed the window"
# apart from "the window couldn't run here".
WINDOW_CLOSED = "closed before sign-in finished"


class WebviewUnavailable(RuntimeError):
    """pywebview isn't importable or has no working backend on this machine."""


# -- in-process: the actual window (runs in the child) ----------------------
def _parse_expires(value) -> int | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(parsedate_to_datetime(str(value)).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_cookies(raw) -> list[dict]:
    """
    pywebview hands back SimpleCookie objects (name -> Morsel); some backends
    give http.cookiejar.Cookie. Flatten either into plain dicts.
    """
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, cookiejar.Cookie):
            out.append({
                "name": item.name, "value": item.value or "",
                "domain": item.domain or "", "path": item.path or "/",
                "secure": bool(item.secure), "expires": item.expires,
            })
            continue
        try:
            morsels = item.items()
        except AttributeError:
            continue
        for name, morsel in morsels:
            out.append({
                "name": name, "value": morsel.value or "",
                "domain": morsel["domain"] or "", "path": morsel["path"] or "/",
                "secure": bool(morsel["secure"]),
                "expires": _parse_expires(morsel["expires"]),
            })
    return out


def _has_session(cookies: list[dict]) -> bool:
    have = {c["name"]: c["value"] for c in cookies}
    return bool(have.get(_SESSION_COOKIE)) and bool(have.get(_SECURE_COOKIE))


# Same-origin probe: "is this session actually good?" - the authoritative signal,
# independent of what the session cookies happen to be named this year.
_WHOAMI_JS = """
(function () {
  try {
    var x = new XMLHttpRequest();
    x.open('GET', '/d2l/api/lp/1.0/users/whoami', false);
    x.send(null);
    return x.status === 200 && x.responseText.trim().charAt(0) === '{';
  } catch (e) { return false; }
})();
"""


def run_login_window(base_url: str, *, timeout: float = _WINDOW_TIMEOUT) -> list[dict]:
    """Open the sign-in window and block until the Brightspace session works,
    then return every cookie the window holds for the host. Main-thread only.

    "Works" = the session cookies are present *or* a whoami call succeeds; the
    latter keeps this going even if Brightspace renames its session cookies.
    """
    try:
        import webview  # heavy, and pulls the platform backend
    except Exception as exc:  # noqa: BLE001
        raise WebviewUnavailable(f"pywebview not available: {exc}") from exc

    host = (urlparse(base_url).hostname or "").lower()
    start_url = base_url.rstrip("/") + "/d2l/home"   # bounces to SSO when logged out
    captured: dict[str, list[dict]] = {}

    def _signed_in(window, cookies: list[dict]) -> bool:
        if _has_session(cookies):
            return True
        try:
            return bool(window.evaluate_js(_WHOAMI_JS))
        except Exception:  # noqa: BLE001 - probe unsupported / blocked
            return False

    def _check(window) -> None:
        try:
            url = window.get_current_url() or ""
            if (urlparse(url).hostname or "").lower() != host:
                return
            cookies = _normalize_cookies(window.get_cookies())
            if _signed_in(window, cookies):
                captured["cookies"] = cookies
                window.destroy()
        except Exception:  # noqa: BLE001 - never let a probe kill the window
            log.debug("webview sign-in probe failed", exc_info=True)

    def _on_start(window) -> None:
        window.events.loaded += lambda: _check(window)
        watchdog = threading.Timer(timeout, window.destroy)
        watchdog.daemon = True
        watchdog.start()

    window = webview.create_window("Sign in to Brightspace", start_url,
                                   width=520, height=720)
    try:
        webview.start(_on_start, window, private_mode=True)
    except Exception as exc:  # noqa: BLE001 - no display, no backend, ...
        raise WebviewUnavailable(f"could not start the sign-in window: {exc}") from exc

    if "cookies" not in captured:
        raise WebviewUnavailable(f"the sign-in window was {WINDOW_CLOSED}")
    return captured["cookies"]


# -- in-parent: launch the child and collect the result --------------------
def _child_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]                       # run.py routes argv>1 to the CLI
    return [sys.executable, "-m", "brightspace_sync"]


def _last_json_object(stdout: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def cookies_to_jar(dicts: list[dict]) -> cookiejar.CookieJar:
    jar = cookiejar.CookieJar()
    for d in dicts:
        domain = d.get("domain") or ""
        expires = d.get("expires")
        jar.set_cookie(cookiejar.Cookie(
            version=0, name=d["name"], value=d.get("value") or "",
            port=None, port_specified=False,
            domain=domain, domain_specified=bool(domain),
            domain_initial_dot=domain.startswith("."),
            path=d.get("path") or "/", path_specified=True,
            secure=bool(d.get("secure")),
            expires=int(expires) if expires else None,
            discard=not expires, comment=None, comment_url=None,
            rest={"HttpOnly": None}, rfc2109=False,
        ))
    return jar


def login_via_webview(cfg: Config) -> cookiejar.CookieJar:
    """Spawn the sign-in window in a child process and return the cookie jar it
    captured. Raises AuthError (imported lazily) on any failure."""
    from .auth import AuthError

    argv = _child_argv() + [SUBCOMMAND, cfg.base_url]
    # Don't flash a console window when the frozen Windows build spawns itself.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    log.info("Opening a sign-in window ...")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, creationflags=flags,
                              timeout=_SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise AuthError(
            "The sign-in window timed out.\n"
            "  Try again, or use `browser: cookies_txt` (export a cookies.txt "
            "and point the config at it)."
        )
    except OSError as exc:
        raise AuthError(f"Couldn't open the sign-in window: {exc}")

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        why = tail[-1] if tail else f"exit code {proc.returncode}"
        if WINDOW_CLOSED in why:
            raise AuthError(f"The sign-in window was {WINDOW_CLOSED}.")
        raise AuthError(
            "The sign-in window couldn't be used on this machine.\n"
            f"  {why}\n"
            "  Sign in to Brightspace in your browser and try again, or use "
            "`browser: cookies_txt`."
        )

    payload = _last_json_object(proc.stdout)
    if not payload or not payload.get("cookies"):
        raise AuthError(f"The sign-in window was {WINDOW_CLOSED}.")

    jar = cookies_to_jar(payload["cookies"])
    # The child only reports success once whoami passed, so don't second-guess it
    # on cookie names here - just make sure something usable came back.
    if not any(c.value for c in jar):
        raise AuthError("Sign-in finished but no cookies were captured. Try again.")
    log.info("Captured a Brightspace session from the sign-in window.")
    return jar
