"""Entry point for the frozen (PyInstaller) build. Not used when installed via pip."""

from brightspace_sync.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
