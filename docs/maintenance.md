# Maintenance: what can break, and where the fallback is

This tool depends on things outside the repo. None of them are contractual. The
design point is that **no single external change takes the whole tool down** -
each critical path has a second way through.

## Authentication (the fragile part)

| If this changes… | What breaks | What still works |
| --- | --- | --- |
| Chrome/Edge cookie encryption (e.g. the Chrome 127 "v20" change) | `rookiepy` can't read the cookie DB | The sign-in window opens automatically (`brightspace_sync/webauth.py`) |
| `rookiepy` drops your Python version / stops shipping wheels | frozen build still fine (pinned in `uv.lock`); `pipx` users on a new Python lose the browser read | sign-in window; `browser: cookies_txt` |
| Brightspace **renames** `d2lSessionVal` / `d2lSecureSessionVal` | `rookiepy` path and `_assert_cookies_usable` reject a good session | sign-in window: it completes on a live **`whoami`** call, not on cookie names (`_WHOAMI_JS`), and captures *all* host cookies. `_finish_session(trust_login=True)` skips the name pre-check for that path. |
| Purdue (or another school) turns on **Conditional Access** / "approved browser" | the sign-in window's SSO gets blocked | `rookiepy` reading the real browser, which has the SSO broker/extension |
| `login.microsoftonline.com` redesigns its login pages | nothing - the window only renders them and waits to land back on the Brightspace host | — |
| `pywebview` ships a new major (its `get_cookies()` shape has moved before) | possible breakage | pinned `pywebview<7`; `_normalize_cookies` already accepts both known shapes; bump deliberately |
| macOS/Windows drop synchronous `XMLHttpRequest` in their webview | `whoami` probe returns false | falls back to the cookie-name check (today's behaviour) |
| WebView2 runtime missing (some Win10) / WebKitGTK missing (Linux frozen binary) | window can't open | `rookiepy` (primary everywhere) + a clear error telling the user |

### Keep an eye on
- **`rookiepy`** - bump it when Chrome/Edge change cookie storage again. The
  window covers the gap in the meantime; it's not a reason to rush.
- **`brightspace-sync doctor`** - the auth diagnostic. If reports come in, ask
  for its output first.

## Brightspace API

- Uses the versioned `/d2l/api/{le,lp}/1.0/…` routes (GET only). D2L keeps old
  versions for years.
- **One exception:** content traversal prefers `/d2l/api/le/unstable/{ou}/content/toc`
  (the only route that returns module/topic rich text). `content.py` falls back
  to the stable `content/root` + `structure/` crawl if that route changes or
  disappears - you lose inline descriptions, not the files.
- `whoami` (`lp/1.0/users/whoami`) is the auth check in two places now
  (`auth._verify` and the sign-in window probe). A 404 is reported as
  "your institution may have disabled the LMS API".

## Distribution

- Frozen binaries pin every dependency via `uv.lock`; `pip/pipx` installs float
  within the `pyproject.toml` ranges.
- The sign-in window runs as a **child process** (`<exe> _webview-login <url>`,
  a hidden subcommand) so `pywebview` never shares a thread with the GUI. If the
  CLI parser or `run.py`'s "any arg → CLI" routing is refactored, that path
  breaks - `tests/test_webauth.py::test_webview_login_subcommand_is_wired`
  guards it.
