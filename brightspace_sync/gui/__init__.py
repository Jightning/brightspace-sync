"""Desktop GUI for non-technical users - setup + run with a progress bar.

The downloaded build starts here (see run.py); `brightspace-sync gui` opens it too.
"""

from __future__ import annotations


def run_gui(config_path: str | None = None) -> int:
    from ..util import setup_logging
    from .app import App

    setup_logging()  # so error detail has somewhere to go (frozen build skips cli.main)
    App(config_path).mainloop()
    return 0
