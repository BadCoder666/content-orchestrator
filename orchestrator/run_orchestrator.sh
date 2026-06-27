#!/bin/bash
# Heartbeat runner for the unified Claude scheduler (called by launchd every ~12 min).
# Sources secrets from a private env-file so no token ever lives in the plist
# (same pattern as run_dashboard.sh / .claude-usage.env).
set -a
[ -f "$HOME/.claude-orchestrator.env" ] && . "$HOME/.claude-orchestrator.env"
set +a
cd "/Users/YOURNAME/claude-orchestrator" || exit 1
exec "/Users/YOURNAME/claude-orchestrator/venv/bin/python" -m orchestrator.dispatcher
