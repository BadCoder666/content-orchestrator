"""
Ranking: velocity / acceleration / dedup / project segregation.

Pure arithmetic over the shared store — no Claude. Mirrors the signal-tracker
spec: engagement = likes + 2*reposts + 3*replies; velocity = engagement /
hours_since_posted (hours floored at 0.25). Acceleration compares the latest
velocity against the previous stored velocity to classify a post as it climbs.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

from . import config


def canonical_url(url: str) -> str:
    """Normalize a Reddit URL so the native scraper (www.reddit.com) and the
    in-app surfacer (often old.reddit.com) produce the SAME key for one thread —
    otherwise dedup-by-url would store the same post twice. Host → www.reddit.com,
    query/fragment stripped (utm); path preserved. Non-Reddit URLs untouched."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    host = p.netloc.lower()
    # Exact match or a real reddit subdomain — NOT a lookalike like "myreddit.com".
    if host == "reddit.com" or host.endswith(".reddit.com"):
        # Strip the trailing slash too: the native scraper stores the API
        # permalink form (".../title/") while a browser-captured surfacer URL
        # often omits it — both must map to ONE key or they'd store twice.
        path = p.path.rstrip("/")
        return urllib.parse.urlunsplit((p.scheme or "https", "www.reddit.com", path, "", ""))
    # X / Twitter: fold twitter.com + mobile/www onto x.com, drop query/fragment,
    # so the same tweet captured as x.com vs twitter.com dedups to one row.
    if host in ("x.com", "twitter.com") or host.endswith((".x.com", ".twitter.com")):
        return urllib.parse.urlunsplit((p.scheme or "https", "x.com", p.path.rstrip("/"), "", ""))
    return url


def engagement(sig: dict) -> int:
    return int(sig.get("likes", 0)) + 2 * int(sig.get("reposts", 0)) + 3 * int(sig.get("replies", 0))


def hours_since(posted_ts: str, now: datetime) -> float:
    """Hours between a post's ISO timestamp and `now`, floored at 0.25h."""
    if not posted_ts:
        return 0.25
    try:
        dt = datetime.fromisoformat(posted_ts.replace("Z", "+00:00"))
    except ValueError:
        return 0.25
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta_h = (now - dt).total_seconds() / 3600.0
    return max(delta_h, 0.25)


def velocity(sig: dict, now: datetime) -> float:
    return engagement(sig) / hours_since(sig.get("posted_ts", ""), now)


def classify(new_velocity: float, prev_velocity: float) -> str:
    """Watching → Accelerating (rising) → Peaked (flattening/declining)."""
    if prev_velocity <= 0:
        return "Watching" if new_velocity == 0 else "Accelerating"
    if new_velocity > prev_velocity * 1.05:
        return "Accelerating"
    if new_velocity < prev_velocity * 0.9:
        return "Peaked"
    return "Watching"


def tag_topics(text: str, keywords: list[str]) -> list[str]:
    low = (text or "").lower()
    return [k for k in keywords if k.lower() in low]


def assign_projects(text: str) -> tuple[str, str]:
    """Return (projects_csv, topic_tags_csv) by matching each project's thesis."""
    bh = tag_topics(text, config.COMPANY_KEYWORDS)
    gx = tag_topics(text, config.NEWSLETTER_KEYWORDS)
    projects = [p for p, hit in (("company", bh), ("newsletter", gx)) if hit]
    tags = sorted(set(bh) | set(gx))
    return ",".join(projects), ",".join(tags)


def dedupe(signals: list[dict]) -> list[dict]:
    """One row per url; keep the first (highest-engagement if pre-sorted)."""
    seen: set[str] = set()
    out: list[dict] = []
    for s in signals:
        u = s.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(s)
    return out
