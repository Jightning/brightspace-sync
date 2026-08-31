"""Entry point for the frozen (PyInstaller) build. Not used when installed via pip.

Double-clicking the downloaded file opens the GUI. Passing any argument
(`brightspace-sync sync`, `... doctor`, ...) keeps the old command-line behaviour
so scheduled jobs still work.
"""

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from brightspace_sync.cli import main as cli_main
        return cli_main()

    if sys.platform == "win32":
        try:  # hide the console window behind the GUI
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass

    from brightspace_sync.gui import run_gui
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
