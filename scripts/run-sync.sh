#!/bin/bash
# Wrapper used by the optional scheduled job (launchd) and handy to run by hand.
# Everything it writes stays inside the project's bs_data/ folder.
# Edit PROJECT_DIR if you move the folder.
set -euo pipefail

PROJECT_DIR="$HOME/Desktop/Projects/bs_search"
LOG_DIR="$PROJECT_DIR/bs_data/logs"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"
exec ./.venv/bin/python -m brightspace_sync sync --notify-on-auth-failure "$@" \
    >> "$LOG_DIR/scheduled-run.log" 2>&1
