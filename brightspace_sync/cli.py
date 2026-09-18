from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

from .auth import AuthError, build_session, diagnose
from .client import Brightspace
from .config import _VALID_BROWSERS, Config, load_config
from .enrollments import list_all_courses
from .sync import run_sync
from .util import log, setup_logging

_SECTIONS = ("content", "grades", "news", "assignments", "calendar")


def _interactive() -> bool:
    """A real terminal - so a sign-in window is OK to open. False under cron/launchd."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _notify(msg: str) -> None:
    """
    Desktop notification (used by the scheduled job).
    For macOS (osascript), Linux (notify-send), and Windows (PowerShell toast). 
    """
    import platform
    import subprocess

    title = "brightspace_sync"
    system = platform.system()

    if system == "Darwin":
        cmd = ["osascript", "-e", f'display notification {msg!r} with title {title!r}']
    elif system == "Windows":
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, "
            "Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::"
            "GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            f"$t.GetElementsByTagName('text').Item(0).AppendChild("
            f"$t.CreateTextNode('{title}')) > $null; "
            f"$t.GetElementsByTagName('text').Item(1).AppendChild("
            f"$t.CreateTextNode({msg!r})) > $null; "
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            f"CreateToastNotifier('{title}').Show("
            "[Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        cmd = ["notify-send", title, msg]

    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=10)
    except Exception:
        pass

def _prepare(args) -> Config:
    """Load config, apply CLI overrides, start writing a run log under data_dir."""

    cfg = load_config(args.config)
    if getattr(args, "browser", None):
        cfg.browser = args.browser
    if getattr(args, "profile", None):
        cfg.chrome_profile = args.profile
    try:
        from logging.handlers import RotatingFileHandler
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        logfile = cfg.log_dir / "brightspace_sync.log"
        
        # keep the last 3 runs
        fh = RotatingFileHandler(logfile, maxBytes=5_000_000, backupCount=2, encoding="utf-8")
        if logfile.exists() and logfile.stat().st_size > 0:
            fh.doRollover()
        fh.setLevel(logging.DEBUG)  # file always gets full detail
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except OSError:
        pass
    log.info("brightspace-sync %s", __import__("brightspace_sync").__version__)
    log.info("config:   %s", cfg.source_path)
    log.info("data_dir: %s", cfg.data_dir)
    return cfg

# `list`, displays the courses that can be retrieved
def cmd_list(args) -> int:
    cfg = _prepare(args)
    try:
        session, _ = build_session(cfg, interactive=_interactive())
    except AuthError as exc:
        log.error("%s", exc)
        return 2
    
    bs = Brightspace(session, cfg)
    courses = list_all_courses(bs)
    if not courses:
        print("No course offerings found.")
        return 0
    
    width = max(len(c.code or "") for c in courses) or 4
    print(f"\n{'ID':>9}  {'CODE':<{width}}  {'ACTIVE':<6}  NAME")
    print("-" * (9 + 2 + width + 2 + 6 + 2 + 40))
    for c in courses:
        print(f"{c.id:>9}  {(c.code or ''):<{width}}  "
              f"{('yes' if c.is_active else 'no'):<6}  {c.name}")

    print("\n# ---- paste the ones you want under courses.allow in config.yaml ----")
    for c in courses:
        sel = c.code or c.name
        print(f'    - match: "{sel}"'.ljust(46) + f"  # {c.name}")
    print(f"\n{len(courses)} course(s). Config: {cfg.source_path}\n")
    return 0

# `whoami`, provides Brightspace user information
def cmd_whoami(args) -> int:
    cfg = _prepare(args)
    try:
        _, who = build_session(cfg, interactive=_interactive())
    except AuthError as exc:
        log.error("%s", exc)
        return 2
    print(f"Name:        {who.get('FirstName','')} {who.get('LastName','')}".rstrip())
    print(f"Unique name: {who.get('UniqueName','?')}")
    print(f"User id:     {who.get('Identifier','?')}")
    print(f"Institution: {cfg.base_url}")
    print(f"Courses dir:  {cfg.courses_dir}")
    return 0

# `doctor`, full diagnoses showing which cookies were found
def cmd_doctor(args) -> int:
    cfg = _prepare(args)
    return diagnose(cfg)

# `gui`, the point-and-click window (setup + run) for non-technical users
def cmd_gui(args) -> int:
    from .gui import run_gui
    return run_gui(getattr(args, "config", None))

# `_webview-login`, hidden: open the sign-in window and print its cookies as JSON.
# Spawned as a child process by webauth.login_via_webview (keeps pywebview off
# the GUI's main thread).
def cmd_webview_login(args) -> int:
    from .webauth import run_login_window

    try:
        cookies = run_login_window(args.base_url)
    except Exception as exc:  # noqa: BLE001 - message goes to the parent via stderr
        print(str(exc), file=sys.stderr)
        return 3
    json.dump({"cookies": cookies}, sys.stdout)
    sys.stdout.write("\n")
    return 0

# `setup`, wizard that writes config.yaml
def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        got = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        got = ""
    return got or default

def _ask_yesno(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    ans = _ask(f"{prompt} ({d})").lower()
    if not ans:
        return default
    return ans[0] == "y"

def cmd_setup(args) -> int:
    path = Path(getattr(args, "config", None) or "config.yaml").expanduser()
    print(f"This writes {path}. Ctrl-C to abort.\n")
    if path.exists() and not _ask_yesno(f"{path.name} already exists - overwrite?", False):
        print("Left it alone.")
        return 0

    base_url = _ask("Your school's Brightspace address", "https://purdue.brightspace.com").rstrip("/")
    if not base_url.startswith("https://"):
        base_url = "https://" + base_url.lstrip("/")

    print("\nWhich browser are you signed in to Brightspace with?")
    print("  chrome / edge / brave / firefox / safari, or 'cookies_txt' for a manual export")
    browser = _ask("browser", "chrome").lower()
    if browser not in _VALID_BROWSERS:
        print(f"  '{browser}' is not one of {sorted(_VALID_BROWSERS)} - using chrome")
        browser = "chrome"

    cfg = Config()
    cfg.base_url = base_url
    cfg.browser = browser

    print("\nChecking that we can read your Brightspace session ...")
    courses = []
    try:
        session, who = build_session(cfg, interactive=True)
        print(f"  OK - signed in as {who.get('FirstName','')} {who.get('LastName','')}".rstrip())
        courses = list_all_courses(Brightspace(session, cfg))
    except AuthError as exc:
        print(f"\n  Couldn't authenticate yet:\n  {exc}\n")
        if not _ask_yesno("Save the config anyway so you can fix this and re-run?", True):
            return 2

    mode, allow_block = "all", "    []"
    if courses:
        print(f"\nYou're in {len(courses)} courses:\n")
        for i, c in enumerate(courses, 1):
            print(f"  {i:>3}. {c.code or '':<28} {c.name}")
        pick = _ask("\nWhich to sync? comma-separated numbers, or 'all'", "all").lower()
        if pick not in ("all", "", "*"):
            want = set()
            for tok in pick.replace(" ", "").split(","):
                if tok.isdigit() and 1 <= int(tok) <= len(courses):
                    want.add(int(tok) - 1)
            chosen = [courses[i] for i in sorted(want)]
            if chosen:
                mode = "allowlist"
                allow_block = "\n".join(
                    f'    - id: {c.id}'.ljust(20) + f"  # {c.name}" for c in chosen)

    videos = _ask_yesno("\nDownload embedded lecture videos too? (more bandwidth)", False)

    path.write_text(_config_yaml(base_url, browser, mode, allow_block, videos), "utf-8")
    print(f"\nWrote {path}")
    print("Next:  brightspace-sync sync"
          + ("    (or `brightspace-sync doctor` if auth still fails)" if not courses else ""))
    return 0

def _config_yaml(base_url: str, browser: str, mode: str, allow_block: str, videos: bool) -> str:
    today = _dt.date.today().isoformat()
    return f"""# Written by `brightspace-sync setup` on {today}.
# Full reference: config.example.yaml

base_url: {base_url}
browser: {browser}
chrome_profile:

data_dir: bs_data.   # everything written goes here

include:
  content: true
  grades: true
  news: true
  assignments: true
  calendar: false
  videos: {str(videos).lower()}     # download embedded lecture videos (yt-dlp is bundled)

# Video configs (only when include.videos is true) - see config.example.yaml
# video_quality: 1080       # cap resolution; 720 roughly halves size/time; best = no cap
# video_rate_limit: ""      # e.g. "3M" to throttle; empty = full speed

courses:
  mode: {mode}             # allowlist (only what's listed) | all (everything)
  allow:
{allow_block}
  ignore:
    - match: "Sandbox"
  map: []                  # rename / merge course folders - see config.example.yaml
"""

# `sync`, the main command for retrieving the Brightspace data
def cmd_sync(args) -> int:
    cfg = _prepare(args)
    only = None
    if args.only:
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        bad = only - set(_SECTIONS)
        if bad:
            log.error("unknown --only section(s): %s (valid: %s)",
                      ", ".join(sorted(bad)), ", ".join(_SECTIONS))
            return 2

    outcome = run_sync(
        cfg,
        cli_course_ids=args.course or None,
        only=only,
        force=args.force,
        dry_run=args.dry_run,
        prune=args.prune,
        skip_videos=args.skip_videos,
        transcribe=args.transcribe,
        interactive=_interactive(),
    )

    if outcome.auth_failed:
        if args.notify_on_auth_failure:
            _notify("Brightspace session expired - open Brightspace, sign in, and refresh")
        return 2
    
    if outcome.errors:
        return 1
    
    return 0

# Here are all the arguments for the cli
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brightspace-sync",
        description="Download your own Brightspace course material to a local folder.",
    )
    p.add_argument("-c", "--config", help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--browser", choices=sorted(_VALID_BROWSERS),
                        help="override config.browser for this run")
    common.add_argument("--profile", metavar="NAME",
                        help="Chrome/Edge/Brave profile folder, e.g. 'Default' or 'Profile 1'")

    sub = p.add_subparsers(dest="cmd", required=True,
                           metavar="{list,whoami,doctor,setup,gui,sync}")

    # `list`
    lp = sub.add_parser("list", parents=[common],
                        help="list every course you're enrolled in")
    lp.set_defaults(func=cmd_list)

    # `whoami`
    wp = sub.add_parser("whoami", parents=[common],
                        help="check that authentication works")
    wp.set_defaults(func=cmd_whoami)

    # `doctor`
    dp = sub.add_parser("doctor", parents=[common],
                        help="diagnose cookie / authentication problems")
    dp.set_defaults(func=cmd_doctor)

    # `setup`
    stp = sub.add_parser("setup", help="interactive first-run: writes config.yaml")
    stp.set_defaults(func=cmd_setup)

    # `gui`
    gp = sub.add_parser("gui", help="open the point-and-click window (setup + run)")
    gp.set_defaults(func=cmd_gui)

    # hidden: internal sign-in-window helper, spawned as a child process
    wl = sub.add_parser("_webview-login")
    wl.add_argument("base_url")
    wl.set_defaults(func=cmd_webview_login)

    # `sync`
    sp = sub.add_parser("sync", parents=[common], help="download configured courses")
    sp.add_argument("--course", type=int, action="append", metavar="ID",
                    help="only this org-unit id (repeatable). Overrides config filter")
    sp.add_argument("--only", metavar="a,b",
                    help=f"limit to these sections ({', '.join(_SECTIONS)})")
    sp.add_argument("--force", action="store_true",
                    help="re-download everything, even if it's already present")
    sp.add_argument("--prune", action="store_true",
                    help="delete local files that no longer exist in Brightspace")
    sp.add_argument("--skip-videos", action="store_true",
                    help="don't download videos this run (overrides include.videos)")
    sp.add_argument("--transcribe", action="store_true",
                    help="write a .md transcript next to each lecture video "
                         "(uses the publisher's captions when there are any, "
                         "otherwise local speech recognition)")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would happen without writing")
    sp.add_argument("--notify-on-auth-failure", action="store_true",
                    help="enables notifications (for the scheduled job)")
    sp.set_defaults(func=cmd_sync)

    return p

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "verbose", False))

    try:
        return args.func(args)
    except KeyboardInterrupt:
        log.error("interrupted, partial progress is saved; re-run to continue")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("unexpected error: %s", exc)
        return 1

if __name__ == "__main__":
    sys.exit(main())
