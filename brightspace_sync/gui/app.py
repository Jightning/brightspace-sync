"""The GUI windows"""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..config import default_config_path, find_config, load_config, save_config
from ..util import log
from .course_editor import CourseEditor
from .model import SetupModel
from .tasks import ConnectWorker, SyncWorker, attach_log, detach_log
from .theme import install as install_theme
from .widgets import Collapsible, ScrollFrame, Tooltip

_BROWSERS = ["chrome", "edge", "brave", "firefox", "safari", "webview", "cookies_txt", "auto"]
_QUALITIES = ["best", "1080", "720", "480"]
_SECTIONS = [
    ("content", "Course content & files", "Readings, pages and files posted under Content."),
    ("grades", "Grades", "Your score for each graded item."),
    ("news", "Announcements", "The announcement posts from each course."),
    ("assignments", "Assignment info", "Assignment names, instructions and due dates (not your submissions)."),
    ("calendar", "Calendar", "Calendar events and their dates."),
    ("videos", "Lecture videos", "Recorded lecture videos. These are large and slow to "
                                 "download — leave off unless you really want them."),
]

def _open_folder(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass

def _num(text: str, default: float) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default

class App(tk.Tk):
    def __init__(self, config_path: str | None = None):
        super().__init__()
        self.title("Brightspace Sync")
        self.geometry("780x680")
        self.minsize(700, 560)
        self.pal = install_theme(self)

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self._sync_worker: SyncWorker | None = None
        self._log_handler = None
        self._run_problems: list[tuple[int, str]] = []
        self._courses_from_file = False
        self._connected_name = ""

        self.config_path = Path(config_path).expanduser() if config_path else self._find_existing()
        self.model = SetupModel()
        self._file_cfg = None
        
        try:
            if self.config_path.is_file():
                self._file_cfg = load_config(str(self.config_path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Config", f"Could not read {self.config_path}:\n{exc}")
        if self._file_cfg is not None:
            self.model = SetupModel.from_config(self._file_cfg, [])

        self._container = ttk.Frame(self, padding=12)
        self._container.pack(fill="both", expand=True)
        self._build_connect()
        self._build_setup()
        self._build_run()
        self._show("connect")
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _find_existing(self) -> Path:
        try:
            return Path(find_config(None))
        except BaseException:
            return default_config_path()

    # Screen 1: connect ------------------------------
    def _build_connect(self) -> None:
        self.connect = ttk.Frame(self._container, padding=20)
        ttk.Label(self.connect, text="Step 1 — Connect to Brightspace",
                  style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.connect, justify="left", wraplength=600, style="Muted.TLabel",
                  text="This uses the Brightspace login your web browser already has — "
                       "no password is asked for or stored. Sign in to Brightspace in "
                       "that browser first, then connect here. If it can't read that "
                       "login, a sign-in window opens so you can sign in directly.").pack(
                           anchor="w", pady=(4, 14))

        form = ttk.Frame(self.connect)
        form.pack(anchor="w", fill="x")
        self.v_url = tk.StringVar(value=self.model.base_url)
        self.v_browser = tk.StringVar(value=self.model.browser)
        self.v_profile = tk.StringVar(value=self.model.chrome_profile or "Default")

        ttk.Label(form, text="Brightspace address").grid(row=0, column=0, sticky="w", pady=4)
        e_url = ttk.Entry(form, textvariable=self.v_url, width=42)
        e_url.grid(row=0, column=1, sticky="w", padx=8)
        e_url.bind("<Return>", lambda _e: self._connect())
        Tooltip(e_url, "Your school's Brightspace web address, e.g. "
                       "https://purdue.brightspace.com")

        ttk.Label(form, text="I'm signed in with").grid(row=1, column=0, sticky="w", pady=4)
        cb = ttk.Combobox(form, values=_BROWSERS, textvariable=self.v_browser,
                          state="readonly", width=16)
        cb.grid(row=1, column=1, sticky="w", padx=8)
        Tooltip(cb, "The browser you normally use for Brightspace. 'auto' tries the "
                    "common ones. 'webview' skips the browser and always opens a "
                    "sign-in window. 'cookies_txt' is an advanced manual option.")

        prof_help = ("Leave this as “Default” unless you use more than one profile in "
                     "Chrome/Edge/Brave. To find its name: open a new tab at "
                     "chrome://version (or edge://version / brave://version), look at "
                     "“Profile Path”, and use the last folder — e.g. “Default” or "
                     "“Profile 1”.")
        lbl_prof = ttk.Label(form, text="Browser profile")
        lbl_prof.grid(row=2, column=0, sticky="w", pady=4)
        e_prof = ttk.Entry(form, textvariable=self.v_profile, width=24)
        e_prof.grid(row=2, column=1, sticky="w", padx=8)
        e_prof.bind("<Return>", lambda _e: self._connect())
        Tooltip(lbl_prof, prof_help)
        Tooltip(e_prof, prof_help)

        self.btn_connect = ttk.Button(self.connect, text="Connect", command=self._connect)
        self.btn_connect.pack(anchor="w", pady=14)
        Tooltip(self.btn_connect, "Check that we can read your Brightspace session. "
                                  "Nothing is downloaded yet.")
        self.lbl_conn = ttk.Label(self.connect, text="", justify="left", wraplength=600)
        self.lbl_conn.pack(anchor="w")

        self.conn_error = ttk.Frame(self.connect, padding=(0, 8, 0, 0))
        self.conn_error_box = tk.Text(self.conn_error, height=13, wrap="word",
                                      state="disabled", font=("", 11),
                                      highlightthickness=0)
        self.conn_error_box.pack(fill="both", expand=True)
        self.conn_error_box.tag_configure("hdr", foreground=self.pal["err"],
                                          font=("", 11, "bold"))

    def _hide_conn_error(self) -> None:
        self.conn_error.forget()

    def _connect(self) -> None:
        self._pull()
        self._hide_conn_error()
        if not self.v_url.get().strip().startswith("https://"):
            self.lbl_conn.configure(
                text="The address must start with https:// — fix it and press Connect.",
                foreground=self.pal["err"])
            return
        self.btn_connect.configure(state="disabled")
        self.lbl_conn.configure(text="Connecting…", foreground=self.pal["muted"])
        ConnectWorker(self.model.to_config(), self.q, with_courses=True).start()

    def _on_connected(self, name: str, courses: list) -> None:
        self.btn_connect.configure(state="normal")
        self.lbl_conn.configure(text="")
        self._hide_conn_error()
        self._connected_name = name
        if self._file_cfg is not None and not self._courses_from_file:
            self.model = SetupModel.from_config(self._file_cfg, courses)
            self._courses_from_file = True
        else:
            self._merge_course_list(courses)
        self.course_editor.model = self.model
        self.course_editor.refresh()
        self.lbl_head.configure(text=f"Connected as {name}  ·  {len(courses)} courses")
        self._sync_all_note()
        self._revalidate()
        self._show("setup")

    def _merge_course_list(self, courses: list) -> None:
        from .model import CourseChoice
        prev = {c.id: c for c in self.model.courses}
        merged = []
        for c in courses:
            cc = CourseChoice.from_course(c)
            old = prev.get(c.id)
            if old:
                cc.selected, cc.folder = old.selected, old.folder
                cc.merge_key, cc.subfolder = old.merge_key, old.subfolder
            merged.append(cc)
        self.model.courses = merged

    def _on_connect_failed(self, msg: str) -> None:
        log.error("connect failed: %s", msg)          # full detail -> log/console
        self.btn_connect.configure(state="normal")
        self.lbl_conn.configure(
            text="✗  Couldn't connect. Work through the steps below, then press Connect again.",
            foreground=self.pal["err"])

        # show the user only the plain-language part: drop parenthetical
        # "(last error: <python exception>)" tails and any "TypeName: repr" lines.
        # greedy to the last ')' on the line so nested parens (KeyError(1)) go too.
        clean = re.sub(r" *\(last error:[^\n]*\)", "", msg)
        lines, kept = clean.splitlines(), []
        for ln in lines:
            s = ln.strip()
            if re.match(r"^[A-Za-z_.]+(Error|Exception):", s):
                continue
            kept.append(ln)
        clean = "\n".join(kept).strip()

        box = self.conn_error_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", "Couldn't sign in to Brightspace\n", ("hdr",))
        box.insert("end", "\n" + clean + "\n")
        box.configure(state="disabled")
        self.conn_error.pack(fill="both", expand=True, pady=(10, 0))

    # Screen 2: setup -------------------
    def _build_setup(self) -> None:
        self.setup = ScrollFrame(self._container)
        b = self.setup.body
        pad = dict(padx=2, pady=3)

        head = ttk.Frame(b)
        head.pack(fill="x")
        self.lbl_head = ttk.Label(head, text="Connected", style="Sub.TLabel")
        self.lbl_head.pack(side="left")
        back = ttk.Button(head, text="Change connection", command=lambda: self._show("connect"))
        back.pack(side="right")
        Tooltip(back, "Go back to step 1 to use a different address or browser.")
        ttk.Label(b, text="Step 2 — Set up your download", style="Heading.TLabel").pack(
            anchor="w", pady=(6, 2))
        ttk.Label(b, style="Muted.TLabel", justify="left", wraplength=660,
                  text="Work down the three boxes, then press Save & Run at the bottom. "
                       "Your choices are saved, so next time you can just run again.").pack(
            anchor="w", pady=(0, 8))

        # 1 - data folder
        fol = ttk.LabelFrame(b, text="1.  Where to save", padding=8)
        fol.pack(fill="x", pady=4)
        ttk.Label(fol, style="Muted.TLabel", justify="left", wraplength=640,
                  text="All course files go in this folder. The default is a “bs_data” "
                       "folder next to this app. Running again only fetches what changed.").pack(
            anchor="w", pady=(0, 4))
        drow = ttk.Frame(fol)
        drow.pack(fill="x")
        self.v_dir = tk.StringVar(value=self.model.data_dir)
        e_dir = ttk.Entry(drow, textvariable=self.v_dir)
        e_dir.pack(side="left", fill="x", expand=True)
        br = ttk.Button(drow, text="Browse…", command=self._browse_dir)
        br.pack(side="left", padx=4)
        for w in (e_dir, br):
            Tooltip(w, "Pick any folder you like, or leave the default.")

        # 2 - sections
        sec = ttk.LabelFrame(b, text="2.  What to download", padding=8)
        sec.pack(fill="x", pady=4)
        ttk.Label(sec, style="Muted.TLabel",
                  text="Tick the kinds of material you want a copy of.").pack(
            anchor="w", pady=(0, 4))
        secgrid = ttk.Frame(sec)
        secgrid.pack(anchor="w")
        self.v_inc = {}
        for i, (key, label, hint) in enumerate(_SECTIONS):
            var = tk.BooleanVar(value=self.model.include.get(key, False))
            self.v_inc[key] = var
            cbx = ttk.Checkbutton(secgrid, text=label, variable=var)
            cbx.grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)
            Tooltip(cbx, hint)

        # 3 - courses
        crs = ttk.LabelFrame(b, text="3.  Which courses", padding=8)
        crs.pack(fill="x", pady=4)
        self.v_all = tk.BooleanVar(value=self.model.sync_all)
        all_cb = ttk.Checkbutton(
            crs, variable=self.v_all, command=self._toggle_all,
            text="Sync all my courses (also pick up ones I enroll in later)")
        all_cb.pack(anchor="w")
        Tooltip(all_cb, "On: download every course, and any you join later. "
                        "Off: only the courses you check below.")
        self.lbl_courses_note = ttk.Label(crs, style="Muted.TLabel", text="")
        self.lbl_courses_note.pack(anchor="w", pady=2)
        self.course_editor = CourseEditor(crs, self.model, on_change=self._revalidate)
        self.course_editor.pack(fill="x")

        # advanced
        adv = Collapsible(b, "Advanced")
        adv.pack(fill="x", pady=4)
        a = adv.body
        self.v_delay = tk.StringVar(value=str(self.model.request_delay_seconds))
        self.v_retries = tk.StringVar(value=str(self.model.max_retries))
        self.v_prune = tk.BooleanVar(value=self.model.prune)
        self.v_vq = tk.StringVar(value=self.model.video_quality)
        self.v_vrate = tk.StringVar(value=self.model.video_rate_limit)
        self.v_vtimeout = tk.StringVar(value=str(self.model.video_timeout))
        rows = [
            ("Delay between requests (s)", self.v_delay, None,
             "Pause between calls to Brightspace. Raise it if you get rate-limited; "
             "0.4 is fine normally."),
            ("Max retries", self.v_retries, None,
             "How many times to retry a failed download before giving up on it."),
            ("Video quality cap", self.v_vq, _QUALITIES,
             "Highest video resolution to fetch. Lower = smaller and faster. "
             "Only matters if “Lecture videos” is on."),
            ("Video speed limit", self.v_vrate, None,
             "Cap video download speed, e.g. “3M” for 3 MB/s. Blank = no limit."),
            ("Video timeout (s, 0 = none)", self.v_vtimeout, None,
             "Give up on a single video after this many seconds. 0 means wait as long as it takes."),
        ]
        for i, (label, var, values, hint) in enumerate(rows):
            lb = ttk.Label(a, text=label)
            lb.grid(row=i, column=0, sticky="w", **pad) # type: ignore
            if values:
                w = ttk.Combobox(a, values=values, textvariable=var, state="readonly", width=14)
            else:
                w = ttk.Entry(a, textvariable=var, width=24)
            w.grid(row=i, column=1, sticky="w", **pad)
            Tooltip(lb, hint)
            Tooltip(w, hint)
        prune_cb = ttk.Checkbutton(
            a, text="Delete local files that were removed from Brightspace", variable=self.v_prune)
        prune_cb.grid(row=len(rows), column=0, columnspan=2, sticky="w", **pad) # type: ignore
        Tooltip(prune_cb, "Keeps your folder an exact mirror. Off by default so nothing "
                          "you have is ever deleted.")

        # actions
        ttk.Separator(b).pack(fill="x", pady=(10, 4))
        self.lbl_problems = ttk.Label(b, style="Err.TLabel", justify="left", wraplength=680)
        self.lbl_problems.pack(anchor="w", pady=(0, 2))
        ttk.Label(b, style="Muted.TLabel", justify="left", wraplength=660,
                  text="“Save & Run” will store the settings and starts downloading.").pack(
            anchor="w", pady=(0, 4))
        act = ttk.Frame(b)
        act.pack(fill="x", pady=6)
        self.btn_run = ttk.Button(act, text="Save & Run  ▶", command=self._run)
        self.btn_run.pack(side="left")
        Tooltip(self.btn_run, "Save the settings and start downloading.")
        save_btn = ttk.Button(act, text="Save settings", command=self._save_only)
        save_btn.pack(side="left", padx=6)
        Tooltip(save_btn, "Write these choices to config.yaml without downloading anything.")

    def _browse_dir(self) -> None:
        start = self.v_dir.get() or str(Path.home())
        chosen = filedialog.askdirectory(initialdir=start, title="Choose a folder")
        if chosen:
            self.v_dir.set(chosen)

    def _toggle_all(self) -> None:
        self.model.sync_all = self.v_all.get()
        self._sync_all_note()
        self._revalidate()

    def _sync_all_note(self) -> None:
        self.lbl_courses_note.configure(
            text=("Every course is downloaded — uncheck any you want to skip."
                  if self.v_all.get() else "Only the checked courses are downloaded."))

    def _pull(self) -> None:
        m = self.model
        m.base_url = self.v_url.get().strip()
        m.browser = self.v_browser.get()
        prof = self.v_profile.get().strip()
        m.chrome_profile = "" if prof.lower() == "default" else prof
        if hasattr(self, "v_dir"):
            m.data_dir = self.v_dir.get().strip()
            m.include = {k: v.get() for k, v in self.v_inc.items()}
            m.sync_all = self.v_all.get()
            m.request_delay_seconds = _num(self.v_delay.get(), 0.4)
            m.max_retries = int(_num(self.v_retries.get(), 4))
            m.prune = self.v_prune.get()
            m.video_quality = self.v_vq.get()
            m.video_rate_limit = self.v_vrate.get().strip()
            m.video_timeout = int(_num(self.v_vtimeout.get(), 0))

    def _revalidate(self, *_) -> None:
        self._pull()
        probs = self.model.problems()
        self.lbl_problems.configure(text="\n".join("•  " + p for p in probs))
        self.btn_run.configure(state="disabled" if probs else "normal")

    # Screen 3: run --------------------------
    def _build_run(self) -> None:
        self.run = ttk.Frame(self._container, padding=4)
        ttk.Label(self.run, text="Step 3 — Downloading", style="Heading.TLabel").pack(anchor="w")
        self.pbar = ttk.Progressbar(self.run, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", pady=8)
        self.lbl_status = ttk.Label(self.run, text="Starting…")
        self.lbl_status.pack(anchor="w")

        row = ttk.Frame(self.run)
        row.pack(fill="x", pady=6)
        self.btn_log = ttk.Button(row, text="Show log  ▼", command=self._toggle_log)
        self.btn_log.pack(side="left")
        Tooltip(self.btn_log, "Show the detailed line-by-line progress.")
        self.btn_stop = ttk.Button(row, text="Stop", command=self._stop)
        self.btn_stop.pack(side="right")
        Tooltip(self.btn_stop, "Stop after the current course. What's already downloaded is kept.")

        self.log_box = tk.Text(self.run, height=14, wrap="none", state="disabled",
                               font=("Menlo", 10))
        self.log_box.tag_configure("warn", foreground=self.pal["warn"])
        self.log_box.tag_configure("err", foreground=self.pal["err"])
        self._log_visible = False

        # end-of-run summary of anything that went wrong
        self.problems_frame = ttk.LabelFrame(self.run, text="Warnings & errors", padding=6)
        self.problems_box = tk.Text(self.problems_frame, height=9, wrap="word",
                                    state="disabled", font=("Menlo", 10))
        self.problems_box.pack(fill="both", expand=True)
        self.problems_box.tag_configure("hdr", font=("Menlo", 10, "bold"))
        self.problems_box.tag_configure("warn", foreground=self.pal["warn"])
        self.problems_box.tag_configure("err", foreground=self.pal["err"])

        self.run_done = ttk.Frame(self.run)
        ttk.Button(self.run_done, text="Open folder",
                   command=lambda: _open_folder(self._data_dir())).pack(side="left")
        ttk.Button(self.run_done, text="Back to setup",
                   command=lambda: self._show("setup")).pack(side="left", padx=6)
        ttk.Button(self.run_done, text="Close", command=self._on_close).pack(side="left")

    # Shared -----------------------
    def _show(self, which: str) -> None:
        for name in ("connect", "setup", "run"):
            getattr(self, name).forget()
        target = getattr(self, which)
        target.pack(fill="both", expand=True)

    def _save_only(self) -> None:
        try:
            path = self._save()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc))
            return
        messagebox.showinfo("Saved", f"Settings saved to\n{path}")

    def _save(self) -> Path:
        self._pull()
        return save_config(self.model.to_config(), self.config_path)

    def _run(self) -> None:
        self._pull()
        probs = self.model.problems()
        if probs:
            messagebox.showwarning("Check settings", "\n".join(probs))
            return
        try:
            self._save()
            cfg = load_config(str(self.config_path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Cannot start", str(exc))
            return

        self._run_cfg = cfg
        self._run_problems = []
        self.pbar.configure(value=0)
        self.lbl_status.configure(text="Starting…")
        self._set_log("")
        self.run_done.forget()
        self.problems_frame.forget()
        self.btn_stop.configure(state="normal", text="Stop")
        self._show("run")

        self._log_handler = attach_log(self.q)
        self._sync_worker = SyncWorker(cfg, self.q)
        self._sync_worker.start()

    def _stop(self) -> None:
        if self._sync_worker:
            self._sync_worker.stop()
        self.btn_stop.configure(state="disabled", text="Stopping…")

    def _on_sync_finished(self, outcome=None, error: str | None = None) -> None:
        if self._log_handler:
            detach_log(self._log_handler)
            self._log_handler = None

        self.btn_stop.configure(state="disabled")
        if error:
            self.lbl_status.configure(text="Something went wrong.")
            self._append_log("\nERROR: " + error, logging.ERROR)
        elif outcome.auth_failed: # type: ignore
            self.lbl_status.configure(
                text="Sign-in expired — open Brightspace, sign in, and run again.")
        elif outcome.stopped: # type: ignore
            self.lbl_status.configure(text="Stopped. What was downloaded so far is saved.")
        elif outcome.errors or outcome.file_errors: # type: ignore
            self.lbl_status.configure(
                text=f"Finished with {len(outcome.errors) or outcome.file_errors} " # type: ignore
                     f"problem(s) — {len(outcome.courses)} course(s) done.") # type: ignore
        else:
            self.pbar.configure(value=100)
            self.lbl_status.configure(text=f"Done — {len(outcome.courses)} course(s), " # type: ignore
                                           f"no problems.")

        self._render_problems(outcome, error)
        self.run_done.pack(fill="x", pady=8)

    def _render_problems(self, outcome=None, error: str | None = None) -> None:
        errs: list[str] = []
        if error:
            errs.append(error)
        if outcome is not None:
            for e in outcome.errors:
                if e not in errs:
                    errs.append(e)

        skip = ("could not download", "video download failed")
        warns: list[str] = []
        for lvl, text in self._run_problems:
            if lvl >= logging.ERROR:
                if text not in errs:
                    errs.append(text)
                continue
            t = text.strip()
            if t.lower().startswith(skip) or "see content_index.json" in t:
                continue
            if t not in warns:
                warns.append(t)

        if not errs and not warns:
            self.problems_frame.forget()
            return

        box = self.problems_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        if errs:
            box.insert("end", f"{len(errs)} problem(s) — files/items that failed:\n", ("hdr",))
            for e in errs[:300]:
                box.insert("end", f"  •  {e}\n", ("err",))
            if warns:
                box.insert("end", "\n")
        if warns:
            box.insert("end", f"{len(warns)} warning(s):\n", ("hdr",))
            for w in warns[:300]:
                box.insert("end", f"  •  {w}\n", ("warn",))
        box.configure(state="disabled")
        self.problems_frame.pack(fill="both", expand=True, pady=(8, 0))

    def _toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.log_box.pack(fill="both", expand=True, pady=(6, 0))
            self.btn_log.configure(text="Hide log  ▲")
        else:
            self.log_box.forget()
            self.btn_log.configure(text="Show log  ▼")

    def _set_log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", text)
        self.log_box.configure(state="disabled")

    def _append_log(self, line: str, levelno: int | None = None) -> None:
        tag = ("err",) if levelno and levelno >= logging.ERROR else \
              ("warn",) if levelno and levelno >= logging.WARNING else ()
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[2], msg[1])
                    if msg[1] >= logging.WARNING and len(msg) > 3:
                        self._run_problems.append((msg[1], msg[3]))
                elif kind == "progress":
                    self.pbar.configure(value=msg[1] * 100)
                    self.lbl_status.configure(text=msg[2])
                elif kind == "connected":
                    self._on_connected(msg[1], msg[2])
                elif kind == "connect_failed":
                    self._on_connect_failed(msg[1])
                elif kind == "sync_done":
                    self._on_sync_finished(outcome=msg[1])
                elif kind == "sync_crashed":
                    self._on_sync_finished(error=msg[1])
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _data_dir(self) -> Path:
        cfg = getattr(self, "_run_cfg", None)
        return cfg.data_dir if cfg else Path(self.v_dir.get())

    def _on_close(self) -> None:
        try:
            running = bool(self._sync_worker and self._sync_worker.is_alive())
        except Exception:  # noqa: BLE001 - never let a thread quirk trap the user
            running = False
        if running:
            if not messagebox.askokcancel("Quit", "A download is running. Stop it and quit?"):
                return
            self._sync_worker.stop()
        self.destroy()
