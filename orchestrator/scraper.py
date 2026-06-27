"""
The combined, Chrome-free scraper.

Pulls all reachable sources once, normalizes to signal dicts, and lets rank.py
segregate by project. Network I/O goes through a `Fetcher` so tests inject
fixtures; the default uses stdlib urllib (no third-party HTTP dep).

Coverage (per the agreed design):
  - reddit   : public search.json            (solid)
  - substack : publication RSS /feed         (solid)
  - x        : no-key bridge                  (FRAGILE — disabled until a
               reliable endpoint is confirmed; degrades gracefully)
LinkedIn is intentionally NOT here — it stays an in-app Chrome task that writes
its results straight into signals.db.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from . import config, rank, store


class Fetcher:
    """Default network fetcher. Tests subclass and override `get`/`post`."""

    def get(self, url: str, *, headers: dict | None = None) -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": config.USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def post(self, url: str, data: dict, *, headers: dict | None = None) -> str:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": config.USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")


# --- Reddit OAuth (free "script" app, application-only client_credentials) ---
def reddit_token(fetcher: Fetcher) -> str | None:
    """Fetch an app-only bearer token. Needs only client_id+secret (no Reddit
    password). Returns None if creds absent or the call fails → caller falls
    back to the (often 403'd) public JSON endpoint."""
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return None
    import base64
    cred = f"{config.REDDIT_CLIENT_ID}:{config.REDDIT_CLIENT_SECRET}".encode()
    auth = base64.b64encode(cred).decode()
    try:
        raw = fetcher.post(
            "https://www.reddit.com/api/v1/access_token",
            {"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        return json.loads(raw).get("access_token")
    except Exception:
        return None


def reddit_search_url(sub: str, keyword: str, *, oauth: bool) -> str:
    # oauth.reddit.com serves the API WITHOUT the `.json` suffix (that's a
    # www.reddit.com convention); keeping `.json` on the oauth host 404s and
    # would silently drop every Reddit signal once creds are set.
    q = f"q={urllib.parse.quote(keyword)}&restrict_sr=1&t=week&limit=25"
    if oauth:
        return f"https://oauth.reddit.com/r/{sub}/search?{q}"
    return f"https://www.reddit.com/r/{sub}/search.json?{q}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- source parsers (pure; fed raw text) ------------------------------------
def parse_reddit(raw: str, sub: str) -> list[dict]:
    """Parse a Reddit search.json body into signal dicts."""
    out: list[dict] = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return out
    for child in data.get("data", {}).get("children", []):
        try:  # one malformed record must not discard its siblings
            d = child.get("data", {})
            created = d.get("created_utc")
            posted = (datetime.fromtimestamp(float(created), timezone.utc).isoformat()
                      if isinstance(created, (int, float, str)) and str(created).strip() else "")
            url = rank.canonical_url("https://www.reddit.com" + d.get("permalink", ""))
            out.append({
                "source": "reddit", "url": url, "author": d.get("author", ""),
                "posted_ts": posted, "text": f"{d.get('title','')} {d.get('selftext','')}".strip(),
                "likes": int(d.get("score", 0)), "reposts": 0,
                "replies": int(d.get("num_comments", 0)),
            })
        except (ValueError, TypeError, OSError, AttributeError):
            continue  # AttributeError guards a non-dict child record
    return out


def parse_substack(raw: str) -> list[dict]:
    """Parse an RSS feed body into signal dicts (engagement unknown → 0)."""
    out: list[dict] = []
    try:
        # Some feeds (e.g. nitter forks) emit a BOM/whitespace before the XML
        # declaration, which ET rejects with "declaration not at start". Strip it.
        root = ET.fromstring(raw.lstrip("﻿ \t\r\n"))
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        try:  # one malformed item must not discard the rest of the feed
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not link:
                continue
            out.append({
                "source": "substack", "url": link, "author": "",
                "posted_ts": _rfc822_to_iso(pub),
                "text": f"{title} {desc}".strip(),
                "likes": 0, "reposts": 0, "replies": 0,
            })
        except (ValueError, TypeError):
            continue
    return out


def _rfc822_to_iso(s: str) -> str:
    if not s:
        return ""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


# --- collection -------------------------------------------------------------
def collect(fetcher: Fetcher) -> list[dict]:
    """Fetch every source; return normalized, project-tagged, deduped signals.

    Network failures on any single source are swallowed (logged by caller via
    the returned diagnostics is out of scope) so one dead source can't abort
    the whole scrape.
    """
    raw_signals: list[dict] = []

    # Reddit: only scrape when OAuth creds yield a token. Public JSON is
    # 403-blocked for scripts, so without creds we skip Reddit entirely (no
    # pointless 403 churn). Adding REDDIT_CLIENT_ID/SECRET auto-resumes it.
    token = reddit_token(fetcher)
    if token:
        rhdr = {"Authorization": f"bearer {token}"}
        for sub in config.REDDIT_SUBS:
            for kw in config.REDDIT_KEYWORDS:
                url = reddit_search_url(sub, kw, oauth=True)
                try:
                    raw_signals += parse_reddit(fetcher.get(url, headers=rhdr), sub)
                except Exception:
                    continue

    for feed in config.SUBSTACK_FEEDS:
        try:
            raw_signals += parse_substack(fetcher.get(feed))
        except Exception:
            continue

    # X: no reliable no-key bridge confirmed yet → skip cleanly.
    # (When X_HANDLES is populated, add the bridge fetch here.)

    # tag + dedupe
    tagged: list[dict] = []
    for s in raw_signals:
        projects, tags = rank.assign_projects(s.get("text", ""))
        if not projects:
            continue  # off-thesis for both projects → drop
        s["projects"], s["topic_tags"] = projects, tags
        tagged.append(s)
    return rank.dedupe(tagged)


def run_scrape(fetcher: Fetcher | None = None, *, db_path=None) -> dict:
    """Full scrape → store → rank. Returns a summary dict."""
    fetcher = fetcher or Fetcher()
    db_path = db_path or config.SIGNALS_DB
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    signals = collect(fetcher)
    with store.connect(db_path) as conn:
        for s in signals:
            store.upsert_signal(conn, s, now_iso)
        # rank everything currently in the store
        for row in store.all_signals(conn):
            v = rank.velocity(row, now)
            status = rank.classify(v, row.get("prev_velocity", 0))
            store.set_rank(conn, row["url"], v, status)
        bh = len(store.signals_for(conn, "company"))
        gx = len(store.signals_for(conn, "newsletter"))
    return {"scraped": len(signals), "company": bh, "newsletter": gx}
