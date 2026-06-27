"""
ingest_inbox.py — drain the LinkedIn surfacer's spool into signals.db.

WHY THIS EXISTS
---------------
The LinkedIn surfacer runs as a Cowork (Claude-in-Chrome) task: it reads the user's
logged-in LinkedIn session, captures on-thesis posts, and needs them landed in
the SAME signals.db the native scraper feeds (so rank.py / the digests treat a
LinkedIn signal exactly like a Reddit one).

But that Cowork task runs in a sandbox whose view of this folder is a FUSE
mount, and SQLite writes over that mount fail ("disk I/O error"). So the
surfacer never touches the DB. Instead it DROPS each run as a plain JSON file
into orchestrator/linkedin_inbox/ (creating files over the mount is fine), and
THIS module — run natively by the dispatcher heartbeat via the real venv, where
SQLite works — picks those files up and stores them. One writer (native) owns
the DB; the producer/consumer hand off through a spool directory, so there is
no write race and no whole-file copy-back.

A record is the same shape store_signal accepts:
    {"author","url","posted","text","likes","reposts","replies"}
('posted' ISO 8601; counts optional, default 0.) Storage uses the exact same
rank+store path as store_signal, so tagging / dedup-by-url / velocity / status
are identical regardless of which door a signal came in.

Idempotent: each spool file is moved to _processed/ after a successful pass, and
store.upsert_signal dedups by url — so a re-dropped or re-processed record can
never create a duplicate row.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, rank, store

# Spool roots under the orchestrator dir (next to signals.db). Each in-app
# surfacer drops *.json into its source's inbox; drain() archives into
# _processed/ and _failed/ derived from that dir. One inbox per Chrome-only
# source (LinkedIn, X) — both ingest through the identical rank+store path.
_ROOT = config.SIGNALS_DB.parent
INBOXES = {
    "linkedin": _ROOT / "linkedin_inbox",
    "x": _ROOT / "x_inbox",
}
INBOX_DIR = INBOXES["linkedin"]  # back-compat alias


def _store_record(conn, r: dict, source: str = "linkedin") -> str:
    """Store ONE record through the orchestrator's own rank+store logic.
    Returns a short status string. Mirrors store_signal.main exactly."""
    text = r.get("text", "")
    projects, tags = rank.assign_projects(text)
    if not projects:
        return "off-thesis"
    url = rank.canonical_url(r["url"])
    sig = {
        "source": source, "url": url, "author": r.get("author", ""),
        "posted_ts": r.get("posted") or r.get("posted_ts") or "", "text": text,
        "likes": int(r.get("likes", 0) or 0),
        "reposts": int(r.get("reposts", 0) or 0),
        "replies": int(r.get("replies", 0) or 0),
        "projects": projects, "topic_tags": tags,
    }
    now = datetime.now(timezone.utc)
    v = rank.velocity(sig, now)
    store.upsert_signal(conn, sig, now.isoformat())
    row = conn.execute("SELECT prev_velocity FROM signals WHERE url=?", (url,)).fetchone()
    prev = row["prev_velocity"] if row else 0
    store.set_rank(conn, url, v, rank.classify(v, prev))
    return "stored"


def _load_records(path: Path) -> list[dict]:
    """A spool file is either a JSON array of records or a single record."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    raise ValueError(f"{path.name}: expected JSON object or array")


def drain(source: str = "linkedin", *, inbox_dir: Path | None = None,
          db_path: Path | None = None, dry_run: bool = False) -> dict:
    """Process every *.json in ONE source's spool (oldest first) into signals.db,
    stamping each row with `source`.

    Each file is handled atomically-ish: all its records are stored in one DB
    transaction, then the file is moved to _processed/. A malformed/erroring
    file is moved to _failed/ so it never blocks the rest of the spool. Files
    whose names start with '_' or '.' are skipped (that's where _processed/ and
    _failed/ live, plus any partial-write temp files).
    """
    inbox = inbox_dir or INBOXES.get(source, INBOX_DIR)
    dbp = db_path or config.SIGNALS_DB
    processed_dir = inbox / "_processed"
    failed_dir = inbox / "_failed"
    inbox.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in inbox.glob("*.json")
                   if not p.name.startswith(("_", ".")))
    summary = {"job": f"{source}_inbox_drain", "source": source,
               "files": len(files), "stored": 0, "off_thesis": 0, "failed": 0}
    if not files:
        return summary
    if dry_run:
        summary["note"] = "dry-run (no DB writes, no file moves)"
        return summary

    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        try:
            recs = _load_records(f)
            stored = off = 0
            with store.connect(dbp) as conn:
                for r in recs:
                    if not r.get("url"):
                        off += 1  # nothing we can dedup on; treat as skip
                        continue
                    if _store_record(conn, r, source) == "stored":
                        stored += 1
                    else:
                        off += 1
            summary["stored"] += stored
            summary["off_thesis"] += off
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.move(str(f), str(processed_dir / f"{stamp}__{f.name}"))
        except Exception as exc:  # one bad file must not block the spool
            summary["failed"] += 1
            summary.setdefault("errors", []).append(f"{f.name}: {exc}")
            try:
                shutil.move(str(f), str(failed_dir / f.name))
            except Exception:
                pass
    return summary


def drain_all(*, dry_run: bool = False) -> dict:
    """Drain every source inbox (LinkedIn, X). One call for the heartbeat job."""
    return {src: drain(src, dry_run=dry_run) for src in INBOXES}


if __name__ == "__main__":
    import sys
    print(json.dumps(drain(dry_run="--dry-run" in sys.argv), indent=2))
