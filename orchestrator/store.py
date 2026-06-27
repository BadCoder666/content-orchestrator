"""
signals.db — the shared scrape store.

One combined scraper writes normalized signal rows here; each project reads
them back filtered by thesis. SQLite, stdlib only. Idempotent upsert keyed on
(source, url) so re-running a scrape never duplicates a post; engagement
snapshots accumulate in a history table so rank.py can compute day-over-day
velocity.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from . import config


def _local_day(iso: str) -> str:
    """The LOCAL (IST) calendar date of an ISO timestamp. Used to decide whether
    two upserts are the 'same day' — comparing raw UTC date prefixes would split
    the IST 00:00–05:29 sliver across two UTC dates and spuriously roll velocity."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return (iso or "")[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(config.LOCAL_TZ).date().isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- reddit | substack | x
    url           TEXT NOT NULL UNIQUE,
    author        TEXT,
    posted_ts     TEXT,                   -- ISO 8601, source post time
    text          TEXT,
    likes         INTEGER DEFAULT 0,
    reposts       INTEGER DEFAULT 0,
    replies       INTEGER DEFAULT 0,
    topic_tags    TEXT DEFAULT '',        -- csv of matched keywords
    projects      TEXT DEFAULT '',        -- csv subset of {company,newsletter}
    velocity      REAL DEFAULT 0,
    prev_velocity REAL DEFAULT 0,
    status        TEXT DEFAULT 'Watching',-- Watching|Accelerating|Peaked
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY,
    url       TEXT NOT NULL,
    taken_at  TEXT NOT NULL,
    engagement INTEGER NOT NULL,
    FOREIGN KEY (url) REFERENCES signals(url)
);
CREATE INDEX IF NOT EXISTS idx_snap_url ON snapshots(url);
"""


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_signal(conn: sqlite3.Connection, sig: dict, now_iso: str) -> None:
    """Insert a new signal or update engagement on an existing one (by url).

    On update we preserve first_seen and roll the previous velocity forward so
    rank.py can detect acceleration across days.
    """
    cur = conn.execute(
        "SELECT velocity, prev_velocity, first_seen, last_seen FROM signals WHERE url=?",
        (sig["url"],))
    row = cur.fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO signals
               (source,url,author,posted_ts,text,likes,reposts,replies,
                topic_tags,projects,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sig["source"], sig["url"], sig.get("author", ""), sig.get("posted_ts", ""),
             sig.get("text", ""), sig.get("likes", 0), sig.get("reposts", 0),
             sig.get("replies", 0), sig.get("topic_tags", ""), sig.get("projects", ""),
             now_iso, now_iso),
        )
    else:
        # Roll velocity→prev_velocity only on a NEW DAY. Multiple upserts of the
        # same url within one day (cross-path: surfacer + scraper hitting one
        # canonical url, or re-runs) must NOT clobber yesterday's velocity, or
        # acceleration would be computed against ~today's own value and read flat.
        same_day = _local_day(row["last_seen"]) == _local_day(now_iso)
        new_prev = row["prev_velocity"] if same_day else row["velocity"]
        conn.execute(
            """UPDATE signals SET likes=?, reposts=?, replies=?, author=?,
               text=?, topic_tags=?, projects=?, prev_velocity=?, last_seen=?
               WHERE url=?""",
            (sig.get("likes", 0), sig.get("reposts", 0), sig.get("replies", 0),
             sig.get("author", ""), sig.get("text", ""), sig.get("topic_tags", ""),
             sig.get("projects", ""), new_prev, now_iso, sig["url"]),
        )
    engagement = sig.get("likes", 0) + 2 * sig.get("reposts", 0) + 3 * sig.get("replies", 0)
    conn.execute(
        "INSERT INTO snapshots (url, taken_at, engagement) VALUES (?,?,?)",
        (sig["url"], now_iso, engagement),
    )


def set_rank(conn: sqlite3.Connection, url: str, velocity: float, status: str) -> None:
    conn.execute("UPDATE signals SET velocity=?, status=? WHERE url=?", (velocity, status, url))


def signals_for(conn: sqlite3.Connection, project: str, statuses: Iterable[str] | None = None) -> list[dict]:
    q = "SELECT * FROM signals WHERE projects LIKE ?"
    params: list = [f"%{project}%"]
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        q += f" AND status IN ({placeholders})"
        params.extend(statuses)
    q += " ORDER BY velocity DESC"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def all_signals(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY velocity DESC").fetchall()]


def snapshot_history(conn: sqlite3.Connection, url: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT taken_at, engagement FROM snapshots WHERE url=? ORDER BY taken_at", (url,)).fetchall()]
