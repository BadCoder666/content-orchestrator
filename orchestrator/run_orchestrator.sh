#!/bin/bash
# Heartbeat runner for the unified Claude scheduler (called by launchd every ~12 min).
# Sources secrets from a private env-file so no token ever lives in the plist
# (same pattern as run_dashboard.sh / .claude-usage.env).
set -a
[ -f "$HOME/.content-orchestrator.env" ] && . "$HOME/.content-orchestrator.env"
set +a
cd "/Users/YOURNAME/content-orchestrator" || exit 1
exec "/Users/YOURNAME/content-orchestrator/venv/bin/python" -m orchestrator.dispatcher
