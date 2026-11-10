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

- Discovers versions at `/d2l/api/{le,lp}/versions/`, whose `SupportedVersions`
  contains strings, and uses the numerically latest 1.x version (GET only).
  Falls back to 1.0 if discovery is unavailable.
- Calendar filters use UTC timestamps with milliseconds (`...29.000Z`): Purdue
  returns HTTP 400 when that component is omitted. Calendar results are
  `ObjectListPage` objects: follow `Next` URLs without reapplying query parameters.
  The shared paging helper also supports enrollment bookmarks and legacy arrays.
- **One exception:** content traversal prefers `/d2l/api/le/unstable/{ou}/content/toc`
  (the only route that returns module/topic rich text). `content.py` falls back
  to the stable `content/root` + `structure/` crawl if that route changes or
  disappears - you lose inline descriptions, not the files.
- `whoami` (`lp/1.0/users/whoami`) is the auth check in two places now
  (`auth._verify` and the sign-in window probe). A 404 is reported as
  "your institution may have disabled the LMS API".
- Content sync also retrieves `/{ou}/overview` and, when present,
  `/{ou}/overview/attachment`. A 404 means absent; a 403 means permission denied
  for this optional resource and preserves previous overview files from pruning.
  A 401 still stops the run as expired authentication. Overviews have no change
  timestamp, so refresh them on every run.
- `navigation.py` extracts course navbar anchors and `d2l-menu-item-link` items
  from `/d2l/le/content/{ou}/Home`, deduplicating desktop/mobile copies. Markup
  changes can affect discovery: missing/unavailable navigation logs a warning
  and preserves previous shortcuts. `external.py` can now resolve Simple Syllabus
  launches and download its syllabus PDF; unsupported tools remain online links.
  The resolver follows the supplied iframe `launchLocation`, the two hidden LTI
  forms (`/ui/lti-login`, `/ui/lti-launch`), and `sso-redirect?doc_url=...`. It never
  executes JavaScript or submits arbitrary forms. The provider's `doc-pdf` route
  can return JSON containing an exact, signed S3 URL; validate that target against
  the provider's tenant-specific syllabus storage prefix and never persist it.
- `external.py` also accepts direct documents from configured exact HTTPS hosts.
  Each redirect is checked against the host policy and public IP resolution;
  document type and signature are validated before committing a file. Provider
  sessions are separate from the Brightspace session. Network errors must not
  print full URLs or response bodies containing launch credentials.
- `limits.py` owns one budget per sync, shared by regular downloads, external
  files, images, media, captions and temporary audio. `DownloadStopped` deliberately
  inherits `BaseException`: broad optional-download exception handlers must not
  swallow a declined cap and continue through a fallback. `run_sync` catches it,
  stops before pruning, and saves the in-progress course manifest in `finally`.
  The CLI approval callback requires a terminal; the app sends a request to the
  Tk main thread and waits on a reply queue, with cancellation unblocking it.
  Approvals are ephemeral and scoped to a run or file, never persisted in config.
- yt-dlp must use the hooked in-process/native path when a budget is active.
  Merged video size is checked before merging; unmetered external protocols and
  automatic installation of an uncached Whisper model stop explicitly. Keep
  temporary-audio/caption hooks and tests in sync with the video path. Metadata
  parsing has an independent 2 MiB bound. Storage estimates are conservative
  (existing data plus transfer bytes and merge workspace); indexes/logs are not
  file-transfer bytes. Do not add a silent max-filesize option to yt-dlp.
- Overview and navigation manifest entries belong to `content` for `--prune`.
  External copies use `external:` keys in the same section. Failed or disabled
  downloads retain earlier copies; unavailable navigation preserves them too.
  Index nodes use `offline_path` for downloaded copies and retain `source_url`.
  `Content/index.html` links saved files relative to the course folder, so the
  folder remains movable. Description HTML must not use a remote `<base>` tag:
  that would send downloaded `_assets/` images back to the server. Existing
  description files are regenerated with the updated wrapper on change or
  `--force`.

## Distribution

- Frozen binaries pin every dependency via `uv.lock`; `pip/pipx` installs float
  within the `pyproject.toml` ranges.
- The sign-in window runs as a **child process** (`<exe> _webview-login <url>`,
  a hidden subcommand) so `pywebview` never shares a thread with the GUI. If the
  CLI parser or `run.py`'s "any arg → CLI" routing is refactored, that path
  breaks - `tests/test_webauth.py::test_webview_login_subcommand_is_wired`
  guards it.
