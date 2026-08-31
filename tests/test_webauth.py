import http.cookiejar as cookiejar
import subprocess
from http.cookies import SimpleCookie

import pytest

from brightspace_sync import auth, webauth
from brightspace_sync.auth import AuthError
from brightspace_sync.config import Config


# -- pure helpers ---------------------------------------------------------
def _simple(name, value, domain, *, secure=True):
    c = SimpleCookie()
    c[name] = value
    c[name]["domain"] = domain
    c[name]["path"] = "/"
    c[name]["secure"] = secure
    c[name]["expires"] = None
    return c


def test_normalize_cookies_from_simplecookie():
    raw = [_simple("d2lSessionVal", "sess", "purdue.brightspace.com"),
           _simple("d2lSecureSessionVal", "secure", ".brightspace.com")]
    got = {c["name"]: c for c in webauth._normalize_cookies(raw)}
    assert got["d2lSessionVal"]["value"] == "sess"
    assert got["d2lSecureSessionVal"]["domain"] == ".brightspace.com"
    assert got["d2lSessionVal"]["secure"] is True


def test_has_session_needs_both_cookies():
    both = [{"name": "d2lSessionVal", "value": "a"},
            {"name": "d2lSecureSessionVal", "value": "b"}]
    assert webauth._has_session(both)
    assert not webauth._has_session([{"name": "d2lSessionVal", "value": "a"}])
    assert not webauth._has_session([{"name": "d2lSessionVal", "value": ""},
                                     {"name": "d2lSecureSessionVal", "value": "b"}])


def test_cookies_to_jar_roundtrip():
    jar = webauth.cookies_to_jar([
        {"name": "d2lSessionVal", "value": "v1", "domain": "purdue.brightspace.com",
         "path": "/", "secure": True, "expires": None},
        {"name": "d2lSecureSessionVal", "value": "v2", "domain": ".brightspace.com",
         "path": "/", "secure": True, "expires": None},
    ])
    by_name = {c.name: c for c in jar}
    assert by_name["d2lSessionVal"].value == "v1"
    assert by_name["d2lSecureSessionVal"].domain == ".brightspace.com"
    assert by_name["d2lSecureSessionVal"].secure


def test_last_json_object_ignores_backend_noise():
    stdout = (
        "Gtk-Message: some backend warning\n"
        'not json {\n'
        '{"cookies": [{"name": "d2lSessionVal", "value": "x"}]}\n'
    )
    assert webauth._last_json_object(stdout) == {
        "cookies": [{"name": "d2lSessionVal", "value": "x"}]
    }
    assert webauth._last_json_object("nothing here") is None


def test_webview_login_subcommand_is_wired():
    """The child process invokes `<exe> _webview-login <url>`; keep that route
    alive through CLI refactors."""
    from brightspace_sync.cli import build_parser

    args = build_parser().parse_args([webauth.SUBCOMMAND, "https://x.brightspace.com"])
    assert args.base_url == "https://x.brightspace.com"
    assert args.func.__name__ == "cmd_webview_login"


def test_child_argv_frozen_vs_source(monkeypatch):
    monkeypatch.setattr(webauth.sys, "frozen", False, raising=False)
    assert webauth._child_argv()[1:] == ["-m", "brightspace_sync"]
    monkeypatch.setattr(webauth.sys, "frozen", True, raising=False)
    monkeypatch.setattr(webauth.sys, "executable", "/opt/app/brightspace-sync")
    assert webauth._child_argv() == ["/opt/app/brightspace-sync"]


# -- subprocess launcher ------------------------------------------------
def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_login_via_webview_parses_child_output(monkeypatch):
    good = ('{"cookies": [{"name": "d2lSessionVal", "value": "v1", '
            '"domain": "purdue.brightspace.com", "secure": true, "expires": null}, '
            '{"name": "d2lSecureSessionVal", "value": "v2", '
            '"domain": ".brightspace.com", "secure": true, "expires": null}]}\n')
    monkeypatch.setattr(webauth.subprocess, "run",
                        lambda *a, **k: _completed(0, stdout=good))
    jar = webauth.login_via_webview(Config())
    assert {c.name for c in jar} == {"d2lSessionVal", "d2lSecureSessionVal"}


def test_login_via_webview_child_failure_is_autherror(monkeypatch):
    monkeypatch.setattr(webauth.subprocess, "run",
                        lambda *a, **k: _completed(3, stderr="could not start the sign-in window: no display"))
    with pytest.raises(AuthError) as ei:
        webauth.login_via_webview(Config())
    assert "no display" in str(ei.value)


def test_login_via_webview_timeout_is_autherror(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)
    monkeypatch.setattr(webauth.subprocess, "run", _boom)
    with pytest.raises(AuthError):
        webauth.login_via_webview(Config())


def test_login_via_webview_trusts_child_even_if_cookie_renamed(monkeypatch):
    # child only reports success after whoami passed - a future cookie rename
    # must not make the parent reject a working session.
    renamed = ('{"cookies": [{"name": "d2lSessionValue", "value": "x", '
               '"domain": "purdue.brightspace.com"}]}\n')
    monkeypatch.setattr(webauth.subprocess, "run",
                        lambda *a, **k: _completed(0, stdout=renamed))
    jar = webauth.login_via_webview(Config())
    assert [c.name for c in jar] == ["d2lSessionValue"]


def test_login_via_webview_empty_jar_is_autherror(monkeypatch):
    empty = '{"cookies": [{"name": "x", "value": "", "domain": "brightspace.com"}]}\n'
    monkeypatch.setattr(webauth.subprocess, "run",
                        lambda *a, **k: _completed(0, stdout=empty))
    with pytest.raises(AuthError):
        webauth.login_via_webview(Config())


# -- build_session fallback wiring ------------------------------------
@pytest.fixture
def cfg():
    c = Config()
    c.base_url = "https://purdue.brightspace.com"
    return c


@pytest.fixture(autouse=True)
def _clear_cache():
    auth._webview_jar_cache.clear()
    yield
    auth._webview_jar_cache.clear()


def _good_jar():
    return webauth.cookies_to_jar([
        {"name": "d2lSessionVal", "value": "v1", "domain": "purdue.brightspace.com",
         "path": "/", "secure": True, "expires": None},
        {"name": "d2lSecureSessionVal", "value": "v2", "domain": ".brightspace.com",
         "path": "/", "secure": True, "expires": None},
    ])


def test_build_session_falls_back_to_webview_when_interactive(monkeypatch, cfg):
    monkeypatch.setattr(auth, "_load_jar_from_browser",
                        lambda *a, **k: (_ for _ in ()).throw(AuthError("no session in browser")))
    monkeypatch.setattr(auth, "_verify", lambda session, cfg: {"FirstName": "Sam"})
    monkeypatch.setattr(auth, "_assert_cookies_usable", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr("brightspace_sync.webauth.login_via_webview",
                        lambda c: calls.append(1) or _good_jar())

    session, who = auth.build_session(cfg, interactive=True)
    assert who["FirstName"] == "Sam"
    assert calls == [1]
    # cached: a second call must not open another window
    auth.build_session(cfg, interactive=True)
    assert calls == [1]


def test_build_session_double_failure_leads_with_primary_steps(monkeypatch, cfg):
    primary = AuthError(
        "No logged-in Brightspace session found in your browser.\n"
        "  1. Open the browser and sign in, then refresh.\n"
        "  2. Try another browser.\n"
        "  4. Or export cookies.txt and set `browser: cookies_txt`."
    )
    monkeypatch.setattr(auth, "_load_jar_from_browser",
                        lambda *a, **k: (_ for _ in ()).throw(primary))
    monkeypatch.setattr("brightspace_sync.webauth.login_via_webview",
                        lambda c: (_ for _ in ()).throw(
                            AuthError("The sign-in window was closed before sign-in finished.")))

    with pytest.raises(AuthError) as ei:
        auth.build_session(cfg, interactive=True)

    text = str(ei.value)
    # automatic-connection steps first, verbatim
    assert text.startswith("No logged-in Brightspace session found in your browser.")
    assert "1. Open the browser and sign in" in text
    assert "cookies.txt" in text
    # window failure is a short trailing line, framed as "closed"
    assert "closed before sign-in finished" in text
    assert text.index("1. Open the browser") < text.index("closed before sign-in finished")


def test_build_session_double_failure_shows_real_window_error(monkeypatch, cfg):
    monkeypatch.setattr(auth, "_load_jar_from_browser",
                        lambda *a, **k: (_ for _ in ()).throw(AuthError("primary boom\n  1. do a thing")))
    monkeypatch.setattr("brightspace_sync.webauth.login_via_webview",
                        lambda c: (_ for _ in ()).throw(
                            AuthError("The sign-in window couldn't be used on this machine.\n  no display")))
    with pytest.raises(AuthError) as ei:
        auth.build_session(cfg, interactive=True)
    text = str(ei.value)
    assert text.startswith("primary boom")
    assert "no display" in text


def test_build_session_no_fallback_when_not_interactive(monkeypatch, cfg):
    monkeypatch.setattr(auth, "_load_jar_from_browser",
                        lambda *a, **k: (_ for _ in ()).throw(AuthError("no session in browser")))
    monkeypatch.setattr("brightspace_sync.webauth.login_via_webview",
                        lambda c: pytest.fail("should not open a window"))
    with pytest.raises(AuthError):
        auth.build_session(cfg, interactive=False)


def test_build_session_cookies_txt_never_falls_back(monkeypatch, cfg):
    cfg.browser = "cookies_txt"
    monkeypatch.setattr(auth, "_load_from_cookies_txt",
                        lambda c: (_ for _ in ()).throw(AuthError("bad cookies.txt")))
    monkeypatch.setattr("brightspace_sync.webauth.login_via_webview",
                        lambda c: pytest.fail("should not open a window"))
    with pytest.raises(AuthError):
        auth.build_session(cfg, interactive=True)


def test_build_session_browser_webview_goes_straight_to_window(monkeypatch, cfg):
    cfg.browser = "webview"
    monkeypatch.setattr(auth, "_load_jar_from_browser",
                        lambda *a, **k: pytest.fail("should not touch the browser"))
    monkeypatch.setattr(auth, "_verify", lambda session, cfg: {})
    monkeypatch.setattr(auth, "_assert_cookies_usable", lambda *a, **k: None)
    monkeypatch.setattr("brightspace_sync.webauth.login_via_webview", lambda c: _good_jar())
    auth.build_session(cfg, interactive=False)


def test_webview_path_skips_cookie_name_precheck(monkeypatch, cfg):
    # simulate Brightspace renaming its cookies: _assert_cookies_usable would
    # raise, but a watched sign-in + whoami must still be accepted.
    cfg.browser = "webview"
    monkeypatch.setattr(auth, "_assert_cookies_usable",
                        lambda *a, **k: pytest.fail("pre-check must be skipped after a watched sign-in"))
    monkeypatch.setattr(auth, "_verify", lambda session, cfg: {"FirstName": "Ada"})
    monkeypatch.setattr("brightspace_sync.webauth.login_via_webview", lambda c: _good_jar())
    _, who = auth.build_session(cfg, interactive=False)
    assert who["FirstName"] == "Ada"
