"""
The scheduler heart: a tiny cron table + an idempotency ledger.

One launchd heartbeat fires dispatcher.py every ~12 min. Each tick we ask every
Job whether it is due *now* (local time), and a ledger ensures once-per-slot
jobs fire exactly once per day/week even though the heartbeat fires many times.
Window jobs (the approval poller) run on every tick inside their window.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable

from . import config


class Ledger:
    """Persists scheduler state so we never double-fire.

    Two namespaces:
      slots : job -> last slot it ran (once-per-day/week idempotency)
      seen  : one-shot keys already acted on (approval ts, daily Slack posts)
              — this is what stops the 06:00-08:30 poller re-queuing the same
              approval on every heartbeat, and stops a retried daily job
              re-posting a digest it already sent.
    Writes are atomic (temp + os.replace) so an overlapping tick can't corrupt
    the file and silently wipe all slot memory.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or (config.STATE_DIR / "run-ledger.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reload()

    def _reload(self) -> None:
        try:
            self._d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._d = {}
        self._d.setdefault("slots", {})
        # seen is a {key: added-date} map, age-bounded (NOT a flat capped list).
        # A flat 2000-cap FIFO mixed approval-ts keys with daily-idempotency keys,
        # so a busy run of approvals could FIFO-evict a `daily:digest:DATE` key and
        # let a retried job re-post. Time-bounding keeps each key for its lifetime.
        seen = self._d.get("seen")
        if isinstance(seen, dict):
            # drop non-string values (external corruption) so the age-prune's
            # string compare can't TypeError and crash the calling tick.
            self._d["seen"] = {k: v for k, v in seen.items() if isinstance(v, str)}
        else:
            self._d["seen"] = {k: "" for k in seen} if isinstance(seen, list) else {}

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._d, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic on POSIX

    def has_run(self, job: str, slot: str) -> bool:
        return self._d["slots"].get(job) == slot

    def mark(self, job: str, slot: str) -> None:
        self._reload()  # pick up a concurrent tick's writes before overwriting
        self._d["slots"][job] = slot
        self._flush()

    def seen(self, key: str) -> bool:
        return key in self._d["seen"]

    def add_seen(self, key: str, *, keep_days: int = 21) -> None:
        self._reload()
        today = datetime.now(config.LOCAL_TZ).date()
        self._d["seen"][key] = today.isoformat()
        # Prune by age, not count: drop keys older than keep_days. ISO dates sort
        # chronologically, so a string compare is enough. Legacy keys with "" date
        # are dropped on first prune (one-time, harmless).
        cutoff = (today - timedelta(days=keep_days)).isoformat()
        self._d["seen"] = {k: v for k, v in self._d["seen"].items() if v >= cutoff}
        self._flush()


@dataclass
class Job:
    name: str
    fn: Callable[[dict], dict]          # fn(ctx) -> summary
    kind: str                            # daily_once | weekly_once | window_repeat
    at: time | None = None               # for *_once
    weekday: int | None = None           # for weekly_once (Mon=0 .. Sun=6)
    window: tuple[time, time] | None = None  # for window_repeat

    def slot(self, now: datetime) -> str:
        return now.date().isoformat()

    def due(self, now: datetime, ledger: Ledger) -> bool:
        if self.kind == "daily_once":
            return now.time() >= self.at and not ledger.has_run(self.name, self.slot(now))
        if self.kind == "weekly_once":
            return (now.weekday() == self.weekday and now.time() >= self.at
                    and not ledger.has_run(self.name, self.slot(now)))
        if self.kind == "window_repeat":
            start, end = self.window
            return start <= now.time() <= end
        return False

    def is_once(self) -> bool:
        return self.kind in ("daily_once", "weekly_once")


def now_local() -> datetime:
    return datetime.now(config.LOCAL_TZ)
