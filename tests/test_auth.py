import http.cookiejar as cookiejar

import rookiepy

from brightspace_sync import auth


def _cookie(name: str, domain: str):
    return rookiepy.create_cookie(domain, "/", True, None, name, "val", True)


def _jar(*pairs):
    jar = cookiejar.CookieJar()
    for name, domain in pairs:
        jar.set_cookie(_cookie(name, domain))
    return jar


def test_registrable_domain():
    assert auth._registrable_domain("purdue.brightspace.com") == "brightspace.com"
    assert auth._registrable_domain("brightspace.nyu.edu") == "nyu.edu"
    assert auth._registrable_domain("localhost") == "localhost"


def test_filter_jar_keeps_d2l_cookies_for_vanity_domain():
    jar = _jar(
        ("d2lSessionVal", "brightspace.nyu.edu"),
        ("d2lSecureSessionVal", ".nyu.edu"),        # scoped wider than the host
        ("shib", ".login.nyu.edu"),
        ("junk", "tracker.example.com"),
    )
    kept = {c.name for c in auth._filter_jar(jar, "brightspace.nyu.edu")}
    assert {"d2lSessionVal", "d2lSecureSessionVal"} <= kept
    assert "junk" not in kept


def test_filter_jar_keeps_brightspace_com_secure_cookie():
    jar = _jar(
        ("d2lSessionVal", "purdue.brightspace.com"),
        ("d2lSecureSessionVal", ".brightspace.com"),
    )
    kept = {c.name for c in auth._filter_jar(jar, "purdue.brightspace.com")}
    assert kept == {"d2lSessionVal", "d2lSecureSessionVal"}


def test_browser_loaders_use_rookiepy():
    order = [name for name, _ in auth._browser_loaders("auto")]
    assert order[:4] == ["chrome", "edge", "brave", "firefox"]
    _, loader = next(iter(auth._browser_loaders("chrome")))
    assert loader is rookiepy.chrome
