"""
CLI used by the in-app surfacers (LinkedIn, Reddit) to push ONE browser-read
signal into the shared signals.db — tagged/deduped/ranked exactly like the
native scraper's rows. Keeps the Chrome-only sources (LinkedIn always; Reddit
when the OAuth captcha is unsolved) in the same store so ranking and drafting
stay centralized. Dedup is by url, so this can run alongside the native scraper
without creating duplicates.

    python -m orchestrator.store_signal --source reddit --url URL --author A \
        --posted ISO --text "..." --likes 12 --reposts 0 --replies 8
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from . import config, rank, store

SOURCES = ("reddit", "linkedin", "substack", "x")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=SOURCES)
    ap.add_argument("--url", required=True)
    ap.add_argument("--author", default="")
    ap.add_argument("--posted", default="", help="ISO 8601 post timestamp")
    ap.add_argument("--text", default="")
    ap.add_argument("--likes", type=int, default=0)
    ap.add_argument("--reposts", type=int, default=0)
    ap.add_argument("--replies", type=int, default=0)
    args = ap.parse_args(argv)

    projects, tags = rank.assign_projects(args.text)
    if not projects:
        print("off-thesis for both projects; not stored")
        return 0

    url = rank.canonical_url(args.url)  # so old.reddit.com dedups vs native www.reddit.com
    sig = {
        "source": args.source, "url": url, "author": args.author,
        "posted_ts": args.posted, "text": args.text,
        "likes": args.likes, "reposts": args.reposts, "replies": args.replies,
        "projects": projects, "topic_tags": tags,
    }
    now = datetime.now(timezone.utc)
    v = rank.velocity(sig, now)
    with store.connect(config.SIGNALS_DB) as conn:
        store.upsert_signal(conn, sig, now.isoformat())
        # Use the persisted prior velocity (upsert rolled it into prev_velocity)
        # so a re-surfaced post can correctly become Peaked — not a hardcoded 0.
        row = conn.execute("SELECT prev_velocity FROM signals WHERE url=?", (url,)).fetchone()
        prev = row["prev_velocity"] if row else 0
        store.set_rank(conn, url, v, rank.classify(v, prev))
    print(f"stored {args.source} signal for projects={projects}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
