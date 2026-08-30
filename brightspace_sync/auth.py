"""Build an authenticated requests.Session from an existing browser login.

Design notes / safety:
  * We never see or ask for a password. We only borrow the session cookies that
    your browser already holds after you logged into Brightspace normally
    (including the Microsoft SSO + MFA dance).
  * `d2lSecureSessionVal` is HttpOnly + Secure, so it cannot be read from JS / a
    bookmarklet - it has to come from the browser cookie store or a manual
    DevTools export. That is why this reads the browser DB directly.
  * If cookie extraction fails, fall back to a Netscape `cookies.txt` you
    export yourself (see README).

Why a registrable-domain filter (the `_registrable_domain` bit below):
  browser_cookie3 asks the browser's cookie DB with `host_key LIKE '%<domain>%'`.
  Brightspace scopes `d2lSessionVal` to `purdue.brightspace.com` but
  `d2lSecureSessionVal` to `.brightspace.com`. Passing the full host drops the
  secure cookie at the SQL level, and Brightspace then answers 403. So we query
  with `brightspace.com` and narrow the result ourselves in `_filter_jar`.
"""

from __future__ import annotations

import http.cookiejar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests

from .config import Config
from .util import log

# A generic desktop User Agent
# Brightspace is lenient, but some WAFs flag the default python-requests UA
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SESSION_COOKIE = "d2lSessionVal"
_SECURE_COOKIE = "d2lSecureSessionVal"

# macOS profile roots for the Chromium-family browsers, used by chrome_profile.
_CHROME_FAMILY_ROOTS = {
    "chrome": "~/Library/Application Support/Google/Chrome",
    "edge": "~/Library/Application Support/Microsoft Edge",
    "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser",
}

class AuthError(RuntimeError):
    pass

# Cookie loading -------------------------------
def _registrable_domain(host: str) -> str:
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host

def _browser_loaders(choice: str):
    """Yields (name, callable) cookie loaders"""
    import browser_cookie3 as bc3

    table = {
        "chrome": bc3.chrome,
        "edge": bc3.edge,
        "brave": bc3.brave,
        "firefox": bc3.firefox,
        "safari": bc3.safari,
    }

    order = ["chrome", "edge", "brave", "firefox", "safari"] if choice == "auto" else [choice]
    for name in order:
        callable = table.get(name)
        if callable is not None:
            yield name, callable

def _profile_cookie_file(cfg: Config) -> str | None:
    """Gets cookies-file path when chrome_profile is set"""
    if not cfg.chrome_profile:
        return None
    
    root = _CHROME_FAMILY_ROOTS.get(cfg.browser)
    if not root:
        log.warning("chrome_profile is set but browser=%s is not one of %s. Set browser to one of these to use a profile.", cfg.browser, "/".join(_CHROME_FAMILY_ROOTS.keys()))
        return None

    # checking likely profile folders
    base = Path(root).expanduser() / cfg.chrome_profile
    for path in (base / "Cookies", base / "Network" / "Cookies"):
        if path.is_file():
            return str(path)
        
    raise AuthError(
        f"chrome_profile={cfg.chrome_profile!r}: no Cookies file found under {base}\n"
        f"Run `doctor` to see available profiles."
    )

def list_chrome_profiles(cfg: Config) -> list[str]:
    root = _CHROME_FAMILY_ROOTS.get(cfg.browser if cfg.browser in _CHROME_FAMILY_ROOTS else "chrome")
    out: list[str] = []
    if not root:
        return out
    base = Path(root).expanduser()
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        if (d / "Cookies").is_file() or (d / "Network" / "Cookies").is_file():
            out.append(d.name)
    return out

def _load_jar_from_browser(cfg: Config, query_domain: str) -> tuple[http.cookiejar.CookieJar, str]:
    """Loads cookie jar from the browser, returns (jar, name)"""
    cookie_file = _profile_cookie_file(cfg)
    last_err: Exception | None = None

    for name, loader in _browser_loaders(cfg.browser):
        kwargs = {"domain_name": query_domain}
        if cookie_file and name in _CHROME_FAMILY_ROOTS:
            kwargs["cookie_file"] = cookie_file

        try:
            jar = loader(**kwargs)
        except Exception as exc:
            log.debug("cookie load from %s failed: %s", name, exc)
            last_err = exc
            continue

        if any(c.name == _SESSION_COOKIE and (c.value or "") for c in jar):
            where = f"{name} ({cookie_file})" if cookie_file else name
            log.info("Using %s cookies", where)
            return jar, name

        log.debug("%s: no usable %s cookie (found: %s)", name, _SESSION_COOKIE, sorted({c.name for c in jar}) or "none")

    hint = f" (last error: {last_err})" if last_err else ""
    raise AuthError(
        f"No logged-in Brightspace session found in your browser{hint}.\n"
        f"  1. Open {cfg.base_url}/d2l/home in the browser and sign in, then refresh.\n"
        f"  2. If you use a non-default Chrome profile, set `browser: chrome` and `chrome_profile: \"Profile 1\"` in config.yaml (see `doctor`).\n"
        f"  3. Or export cookies.txt and set `browser: cookies_txt`."
    )

def _load_from_cookies_txt(cfg: Config) -> http.cookiejar.CookieJar:
    path = cfg.cookies_txt_path
    if not path.is_file():
        raise AuthError(f"cookies_txt_path does not exist: {path}")
    jar = http.cookiejar.MozillaCookieJar(str(path))

    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except http.cookiejar.LoadError as exc:
        raise AuthError(f"could not parse {path} as a Netscape cookies.txt: {exc}")

    if not any(c.name == _SESSION_COOKIE and (c.value or "") for c in jar):
        raise AuthError(f"{path} has no usable {_SESSION_COOKIE} cookie")

    log.info("Using cookies from %s", path)
    return jar

def _filter_jar(jar: Iterable[http.cookiejar.Cookie], host: str) -> http.cookiejar.CookieJar:
    """Keep only cookies related to host or 'brightspace.com'"""
    out = http.cookiejar.CookieJar()
    for cookie in jar:
        cookie_domain = (cookie.domain or "").lstrip(".")
        if host == cookie_domain or host.endswith("." + cookie_domain) or cookie_domain.endswith("brightspace.com"):
            out.set_cookie(cookie)

    return out

# Session build and verify ---------------------------------------
def build_session(cfg: Config) -> tuple[requests.Session, dict]:
    """Returns (session, whoami)"""
    host = urlparse(cfg.base_url).hostname or ""

    # load jar
    if cfg.browser == "cookies_txt":
        raw_jar: http.cookiejar.CookieJar = _load_from_cookies_txt(cfg)
    else:
        raw_jar, _ = _load_jar_from_browser(cfg, _registrable_domain(host))

    session = _session_from_jar(raw_jar, host)
    _assert_cookies_usable(session, cfg)
    whoami = _verify(session, cfg)

    return session, whoami

def _session_from_jar(raw_jar: http.cookiejar.CookieJar, host: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept": "application/json, */*"})
    session.cookies = requests.cookies.RequestsCookieJar() # type: ignore

    for c in _filter_jar(raw_jar, host):
        session.cookies.set_cookie(c)

    return session

def _cookie_value(session: requests.Session, name: str) -> str | None:
    for c in session.cookies:
        if c.name == name:
            return c.value
        
    return None

def _assert_cookies_usable(session: requests.Session, cfg: Config) -> None:
    """Ensures that the cookies in the session are usable for Brightspace"""
    sv = _cookie_value(session, _SESSION_COOKIE)
    kv = _cookie_value(session, _SECURE_COOKIE)

    if not sv:
        raise AuthError(
            f"The {_SESSION_COOKIE} cookie came back empty. That usually means the "
            f"browser cookie store could not be decrypted (Keychain access was "
            f"denied, or browser_cookie3 is older than your browser).\n"
            f"Fix: re-run and click 'Allow' on the Keychain prompt, "
            f"`pip install -U browser-cookie3`, or use `browser: cookies_txt`."
        )
    
    if not kv:
        raise AuthError(
            f"Found {_SESSION_COOKIE} but not {_SECURE_COOKIE}. Brightspace needs both, and will 403 without the latter.\n"
            f"Fix: make sure you are fully logged in at {cfg.base_url}/d2l/home in "
            f"the '{cfg.chrome_profile or 'Default'}' profile, then re-run. If it persists, use `browser: cookies_txt`."
        )


def _probe_web_dashboard(session: requests.Session, cfg: Config) -> str:
    """Heuristically checks if the browser session is still good. Return \"logged_in\" | \"logged_out\" | \"unknown\"."""
    try:
        r = session.get(f"{cfg.base_url}/d2l/home", timeout=30, allow_redirects=True)
    except requests.RequestException:
        return "unknown"
    
    final = (r.url or "").lower()
    body = (r.text or "")[:6000].lower()
    login_markers = ("/d2l/login", "login.microsoftonline.com", "/adfs/", "samlrequest", "federationmetadata", 'name="password"', "please log in")

    if any(m in final for m in login_markers) or any(m in body for m in login_markers):
        return "logged_out"
    if r.status_code == 200:
        return "logged_in"

    return "unknown"


def _verify(session: requests.Session, cfg: Config) -> dict:
    """Verifies the session to ensure there are no issues via a test request (otherwise raises an AuthError with fixing steps)"""

    url = f"{cfg.base_url}/d2l/api/lp/1.0/users/whoami"
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as exc:
        raise AuthError(f"could not reach {cfg.base_url}: {exc}")

    if r.status_code in (401, 403):
        state = _probe_web_dashboard(session, cfg)
        if state == "logged_in":
            raise AuthError(
                f"Your browser's Brightspace session works (the web dashboard loaded) but the LMS API refused it (HTTP {r.status_code}).\n"
                f"Run `doctor` to see which cookies were sent. If both "
                f"{_SESSION_COOKIE} and {_SECURE_COOKIE} are present and current, "
                f"this Brightspace instance is blocking cookie-based API access and this program won't work :<"
            )
        raise AuthError(
            f"Brightspace rejected the session (HTTP {r.status_code}) and the web "
            f"dashboard also bounced to login. The cookies are either stale or from the wrong browser/profile.\n"
            f"  1. Open \"{cfg.base_url}/d2l/home\" and sign in (do something like refreshing. or clicking a course).\n"
            f"  2. If you have a non-default Chrome profile, set `chrome_profile` in config (see `doctor`).\n"
            f"  3. If it's still not working, use `browser=\"cookies_txt\"`."
        )
    
    if r.status_code == 404:
        raise AuthError(
            "The Brightspace LMS API (/d2l/api/lp/1.0/users/whoami) returned 404. Your institution may have disabled it."
        )
    
    r.raise_for_status()
    try:
        who = r.json()
    except ValueError:
        raise AuthError("whoami did not return JSON, the session is probably a login redirect. Re-authenticate in the browser.")

    log.info("Authenticated as %s %s (%s), user id %s", who.get("FirstName", "?"), who.get("LastName", ""), who.get("UniqueName", "?"), who.get("Identifier", "?"))

    return who


# doctor ------------------------------------
def _fmt_expiry(epoch: float | None) -> str:
    """Displays expiry timestamp as a readable string"""
    if not epoch:
        return "session"
    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "?"
    
    expired = dt < datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC") + ("  EXPIRED" if expired else "")


def diagnose(cfg: Config) -> int:
    """Print everything relevant to authentication"""
    host = urlparse(cfg.base_url).hostname or ""
    print("\n== config ==")
    print(f"  base_url        {cfg.base_url}")
    print(f"  browser         {cfg.browser}")
    print(f"  chrome_profile  {cfg.chrome_profile or '(default)'}")
    print(f"  data_dir        {cfg.data_dir}")
    print(f"  courses_dir      {cfg.courses_dir}")
    print(f"  cookies_txt     {cfg.cookies_txt_path}  "
        f"({'exists' if cfg.cookies_txt_path.is_file() else 'not present'})")

    if cfg.browser != "cookies_txt":
        profiles = list_chrome_profiles(cfg)
        if profiles:
            print(f"\n== chrome/edge/brave profiles with a cookie store ==\n  " + ", ".join(profiles))

    print("\n== cookies ==")
    try:
        if cfg.browser == "cookies_txt":
            raw_jar = _load_from_cookies_txt(cfg)
        else:
            raw_jar, _ = _load_jar_from_browser(cfg, _registrable_domain(host))
    except AuthError as exc:
        print(f"  FAILED to load cookies:\n  {exc}")
        return 2

    bs_cookies = list(_filter_jar(raw_jar, host))
    if not bs_cookies:
        print("  No brightspace.com cookies after filtering")
        return 2
    print(f"  {'NAME':<24} {'DOMAIN':<24} {'SEC':<4} {'LEN':<5} EXPIRES")
    for c in sorted(bs_cookies, key=lambda x: x.name):
        print(f"  {c.name:<24} {(c.domain or ''):<24} "
              f"{('yes' if c.secure else 'no'):<4} {len(c.value or ''):<5} {_fmt_expiry(c.expires)}")

    session = _session_from_jar(raw_jar, host)
    have_s = bool(_cookie_value(session, _SESSION_COOKIE))
    have_k = bool(_cookie_value(session, _SECURE_COOKIE))
    print(f"\n  {_SESSION_COOKIE}: {'OK' if have_s else 'MISSING/EMPTY'}")
    print(f"  {_SECURE_COOKIE}: {'OK' if have_k else 'MISSING/EMPTY'}")

    print("\n== live check ==")
    try:
        who = _verify(session, cfg)
    except AuthError as exc:
        print(f"  whoami FAILED:\n  {exc}")
        return 2
    
    print(f"  whoami OK -> {who.get('FirstName','')} {who.get('LastName','')} "
          f"({who.get('UniqueName','?')})")
    print("\nAuthentication is working.\n")
    return 0
