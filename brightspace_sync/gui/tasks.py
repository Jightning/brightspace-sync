"""
Background workers + log bridge, so the window never freezes

Everything a worker wants to tell the UI goes through a plain queue of
``(kind, *payload)`` tuples that the App drains on the tk main loop.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from ..auth import AuthError, build_session
from ..client import Brightspace
from ..config import Config
from ..enrollments import list_all_courses
from ..sync import run_sync


class QueueLogHandler(logging.Handler):
    """Feeds formatted log lines into a queue as ``("log", levelno, text)``."""

    def __init__(self, q: "queue.Queue[tuple]") -> None:
        super().__init__(logging.INFO)
        self._q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put(("log", record.levelno, self.format(record), record.getMessage()))
        except Exception:  # pragma: no cover - never let logging crash a run
            pass


def attach_log(q: "queue.Queue[tuple]") -> QueueLogHandler:
    h = QueueLogHandler(q)
    logging.getLogger().addHandler(h)
    return h


def detach_log(h: logging.Handler) -> None:
    logging.getLogger().removeHandler(h)


class ConnectWorker(threading.Thread):
    """Sign in and (optionally) pull the course list. Non-blocking for the UI."""

    def __init__(self, cfg: Config, q: "queue.Queue[tuple]", *, with_courses: bool):
        super().__init__(daemon=True)
        self._cfg, self._q, self._with_courses = cfg, q, with_courses

    def run(self) -> None:
        try:
            session, who = build_session(self._cfg)
        except AuthError as exc:
            self._q.put(("connect_failed", str(exc)))
            return
        except Exception as exc:  # noqa: BLE001
            self._q.put(("connect_failed", f"{type(exc).__name__}: {exc}"))
            return

        name = f"{who.get('FirstName', '')} {who.get('LastName', '')}".strip() or "?"
        courses: list[Any] = []
        if self._with_courses:
            try:
                courses = list_all_courses(Brightspace(session, self._cfg))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("connect_failed", f"Signed in as {name}, but the "
                                               f"course list failed: {exc}"))
                return
        self._q.put(("connected", name, courses))


class SyncWorker(threading.Thread):
    """Runs run_sync, forwarding progress + a stop flag."""

    def __init__(self, cfg: Config, q: "queue.Queue[tuple]"):
        super().__init__(daemon=True)
        self._cfg, self._q = cfg, q
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            outcome = run_sync(
                self._cfg,
                on_progress=lambda frac, msg: self._q.put(("progress", frac, msg)),
                should_stop=self._stop.is_set,
            )
        except Exception as exc:  # noqa: BLE001
            self._q.put(("sync_crashed", f"{type(exc).__name__}: {exc}"))
            return
        self._q.put(("sync_done", outcome))
