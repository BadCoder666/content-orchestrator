#!/bin/bash
# Heartbeat runner for the unified Claude scheduler (called by launchd every ~12 min).
# Sources secrets from a private env-file so no token ever lives in the plist
# (same pattern as run_dashboard.sh / .claude-usage.env).
set -a
[ -f "$HOME/.content-orchestrator.env" ] && . "$HOME/.content-orchestrator.env"
set +a
ROOT="/Users/YOURNAME/content-orchestrator"
cd "$ROOT" || exit 1
PYBIN="$ROOT/venv/bin/python"

# Self-heal: a Homebrew Python upgrade can delete the interpreter the venv's
# python symlink points at, making `$PYBIN` a dead symlink that fails on exec —
# which silently kills the dispatcher on every tick. If the venv python no longer
# runs, rebuild it against a pinned python@3.12 (or any python3 as a fallback) and
# reinstall deps, so this tick repairs instead of dying.
if ! "$PYBIN" -c "import sys" >/dev/null 2>&1; then
    echo "$(date '+%F %T') venv python broken — rebuilding" >&2
    REBUILD_PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
    [ -x "$REBUILD_PY" ] || REBUILD_PY="$(command -v python3)"
    rm -rf "$ROOT/venv"
    "$REBUILD_PY" -m venv "$ROOT/venv" \
        && "$PYBIN" -m pip install -q -r "$ROOT/requirements.txt" >/dev/null 2>&1 || true
fi
exec "$PYBIN" -m orchestrator.dispatcher
