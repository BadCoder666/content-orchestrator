"""
Slack send + poll + approval parsing.

Sending and reading use the Slack Web API (bot token from env). The approval
PARSER is the deterministic core (no Claude) — it turns the user's short replies into
intents. Ported from newsletter_state.parse_reply so channel/pick semantics stay
identical to the retired Cowork tasks.

Bot markers we ignore when scanning for the user's reply (the connector posts as the user,
so we distinguish our own messages by their leading marker).
"""
from __future__ import annotations

import json
import logging
import re
import socket
import urllib.parse
import urllib.request
from typing import Any

from . import config

log = logging.getLogger("orchestrator.slack")


def has_internet(host: str = "slack.com", port: int = 443, timeout: float = 4.0) -> bool:
    """Cheap connectivity probe — a Window B wake with no internet should go
    straight back to sleep rather than attempt (and fail) Slack reads."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False

BOT_MARKERS = ("#", "🌙", "✍️", "✅", "👍", "🤖", "📝", "🚀", "⚠️", "(1/2)", "(2/2)")
_API = "https://slack.com/api/"


# --- deterministic reply parser ---------------------------------------------
_PICK_RE = re.compile(r"^\s*(\d+)\s*(to\s+(x|substack|both))?\s*$", re.I)
_EDIT_RE = re.compile(r"^\s*edit\s*:\s*(.+)$", re.I | re.S)
_APPROVE_RE = re.compile(r"^\s*(R?\d+(?:\s+R?\d+)*)\s*$", re.I)


def parse_reply(text: str, stage: str) -> tuple:
    """Classify a the user reply given the pipeline stage.

    stages: 'awaiting_pick' (digest sent, expecting an idea number/channel),
            'awaiting_approval' (draft/shortlist sent, expecting publish/etc),
            'idle' (free-text → captured as a thought).
    Returns a tuple whose first element is the intent kind.
    """
    t = (text or "").strip()
    low = t.lower()

    if low in ("skip", "pass"):
        return ("skip",)
    if low in ("publish", "post", "go"):
        return ("publish",)
    if low.startswith("publish to "):
        return ("publish", low.split(" to ", 1)[1].strip())
    if low in ("hold", "park", "wait"):
        return ("hold",)
    m = _EDIT_RE.match(t)
    if m:
        return ("edit", m.group(1).strip())

    if stage == "awaiting_pick":
        m = _PICK_RE.match(t)
        if m:
            channel = (m.group(3) or "both").lower()
            return ("pick", int(m.group(1)), channel)

    if stage == "awaiting_approval":
        m = _APPROVE_RE.match(t)
        if m:
            nums = [int(x.lstrip("Rr")) for x in m.group(1).split()]
            return ("approve", nums)

    return ("thought", t)


def is_bot_message(text: str) -> bool:
    t = (text or "").lstrip()
    return any(t.startswith(mk) for mk in BOT_MARKERS)


# --- Slack Web API (network; guarded by dry_run) ----------------------------
def _post(method: str, payload: dict, token: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _API + method, data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(method: str, params: dict, token: str) -> dict:
    """Form-encoded call for Slack's READ methods. conversations.replies (and
    other GET-style methods) reject a JSON body with `invalid_arguments` — they
    must be form-encoded. (chat.postMessage, a write method, is fine with JSON.)"""
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        _API + method, data=data,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(channel: str, text: str, *, token: str | None = None,
                 thread_ts: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    if dry_run or not (token or config.SLACK_BOT_TOKEN):
        print(f"[slack dry-run] → {channel}: {text[:120]}...")
        return {"ok": True, "ts": "dry-run", "dry_run": True}
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        return _post("chat.postMessage", payload, token or config.SLACK_BOT_TOKEN)
    except Exception as exc:  # no internet / Slack down — caller falls back to local notify
        log.warning("Slack chat.postMessage failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def read_channel(channel: str, *, oldest: str | None = None, token: str | None = None,
                 dry_run: bool = False) -> list[dict]:
    if dry_run or not (token or config.SLACK_BOT_TOKEN):
        return []
    payload: dict = {"channel": channel, "limit": 50}
    if oldest:
        payload["oldest"] = oldest
    res = _get("conversations.history", payload, token or config.SLACK_BOT_TOKEN)
    if not res.get("ok"):
        log.warning("Slack conversations.history(%s) FAILED: %s — poller will see "
                    "nothing. Check the bot's scopes/membership.", channel, res.get("error"))
        return []
    return res.get("messages", [])


def read_thread(channel: str, thread_ts: str, *, token: str | None = None,
                dry_run: bool = False) -> list[dict]:
    if dry_run or not (token or config.SLACK_BOT_TOKEN):
        return []
    res = _get("conversations.replies", {"channel": channel, "ts": thread_ts},
               token or config.SLACK_BOT_TOKEN)
    if not res.get("ok"):
        log.warning("Slack conversations.replies(%s) FAILED: %s", channel, res.get("error"))
        return []
    return res.get("messages", [])


def read_channel_deep(channel: str, *, token: str | None = None,
                      dry_run: bool = False) -> list[dict]:
    """Top-level channel messages PLUS thread replies. conversations.history does
    NOT return thread replies, so an approval typed as a threaded reply (the
    natural Slack action on a posted draft) would be invisible to a history-only
    read. This folds each thread's replies in so the poller catches an approval
    whether it's a new channel message or a threaded reply."""
    top = read_channel(channel, token=token, dry_run=dry_run)
    out = list(top)
    for m in top:
        if int(m.get("reply_count", 0) or 0) > 0 and m.get("ts"):
            for r in read_thread(channel, m["ts"], token=token, dry_run=dry_run):
                if r.get("ts") != m.get("ts"):  # the parent is repeated in replies
                    out.append(r)
    return out
