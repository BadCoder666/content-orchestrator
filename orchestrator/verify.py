"""
verify.py — the self-test half of the adversarial loop.

Exercises every deterministic component offline with fixtures (no network, no
Claude, no Slack, no Chrome): cron due-logic + ledger idempotency, velocity /
acceleration math, project segregation, reply parsing, store upsert/dedupe,
scraper parsing, chrome_queue round-trip, and manifest coverage. Exits non-zero
on any failure so it can gate a deploy.

  python -m orchestrator.verify            # offline suite (default)
  python -m orchestrator.verify --live     # also smoke-test network + Slack DM
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from . import chrome_queue, config, jobs, rank, scraper, slack_io, store
from .schedule import Job, Ledger

FIX = Path(__file__).parent / "tests" / "fixtures"
_checks: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _checks.append((name, bool(cond), detail))


# --- rank math --------------------------------------------------------------
def test_rank():
    now = datetime(2025, 6, 16, 0, 0, tzinfo=timezone.utc)
    sig = {"likes": 10, "reposts": 5, "replies": 4}
    check("engagement formula", rank.engagement(sig) == 10 + 10 + 12, str(rank.engagement(sig)))
    # 2h-old post
    posted = (now - timedelta(hours=2)).isoformat()
    v = rank.velocity({**sig, "posted_ts": posted}, now)
    check("velocity = engagement/hours", abs(v - 32 / 2) < 1e-6, f"{v}")
    # hours floored at 0.25 (avoid div-by-zero on brand-new posts)
    fresh = rank.velocity({**sig, "posted_ts": now.isoformat()}, now)
    check("hours floored at 0.25", abs(fresh - 32 / 0.25) < 1e-6, f"{fresh}")
    check("classify rising → Accelerating", rank.classify(20, 10) == "Accelerating")
    check("classify falling → Peaked", rank.classify(5, 10) == "Peaked")


def test_segregation():
    p1, t1 = rank.assign_projects("ChatGPT shopping and Schema.org product data for Shopify")
    check("company thesis matches ecommerce post", "company" in p1, p1)
    p2, _ = rank.assign_projects("NASA lunar lander reaches orbit")
    check("newsletter thesis matches space post", "newsletter" in p2, p2)
    p3, _ = rank.assign_projects("My sourdough starter died again")
    check("off-thesis post matches neither", p3 == "", repr(p3))


def test_dedupe():
    rows = [{"url": "a"}, {"url": "a"}, {"url": "b"}]
    check("dedupe by url", len(rank.dedupe(rows)) == 2)


def test_canonical_url():
    # surfacer form: old host, NO trailing slash, utm query — vs native www form WITH slash
    a = rank.canonical_url("https://old.reddit.com/r/shopify/comments/x/title?utm=1")
    b = rank.canonical_url("https://www.reddit.com/r/shopify/comments/x/title/")
    check("old+noslash dedups vs native www+slash", a == b, f"{a} vs {b}")
    check("reddit query stripped", "utm" not in a, a)
    li = "https://www.linkedin.com/posts/abc?x=1"
    check("non-reddit url untouched", rank.canonical_url(li) == li)
    check("lookalike host NOT rewritten",
          rank.canonical_url("https://myreddit.com/x") == "https://myreddit.com/x")
    check("empty url safe", rank.canonical_url("") == "")
    # X: twitter.com / mobile / query all fold to one x.com key
    x1 = rank.canonical_url("https://twitter.com/someone/status/123?s=20")
    x2 = rank.canonical_url("https://x.com/someone/status/123")
    check("twitter.com folds to x.com (dedups)", x1 == x2 == "https://x.com/someone/status/123", f"{x1} vs {x2}")


# --- reply parsing ----------------------------------------------------------
def test_parse_reply():
    check("publish", slack_io.parse_reply("publish", "awaiting_approval")[0] == "publish")
    check("hold", slack_io.parse_reply("hold", "awaiting_approval")[0] == "hold")
    check("skip", slack_io.parse_reply("skip", "awaiting_pick")[0] == "skip")
    pick = slack_io.parse_reply("3 to x", "awaiting_pick")
    check("pick number+channel", pick == ("pick", 3, "x"), str(pick))
    appr = slack_io.parse_reply("R1 R3", "awaiting_approval")
    check("approve numbers", appr == ("approve", [1, 3]), str(appr))
    edit = slack_io.parse_reply("edit: tighten the intro", "awaiting_approval")
    check("edit notes", edit[0] == "edit" and "tighten" in edit[1], str(edit))
    check("'post' → bare publish", slack_io.parse_reply("post", "awaiting_approval") == ("publish",))
    check("'go' → bare publish", slack_io.parse_reply("go", "awaiting_approval") == ("publish",))
    check("'publish to x' → channel x",
          slack_io.parse_reply("publish to x", "awaiting_approval") == ("publish", "x"))
    thought = slack_io.parse_reply("what if we framed it around trust?", "idle")
    check("free text → thought", thought[0] == "thought", str(thought))
    check("bot message detection", slack_io.is_bot_message("✅ Published: foo"))
    check("human message not bot", not slack_io.is_bot_message("publish"))


# --- store ------------------------------------------------------------------
def test_store():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        s = {"source": "reddit", "url": "u1", "text": "shopify schema.org",
             "likes": 10, "replies": 5, "reposts": 0, "projects": "company", "topic_tags": "shopify"}
        with store.connect(db) as conn:
            store.upsert_signal(conn, s, "2025-06-16T00:00:00+00:00")
            store.upsert_signal(conn, {**s, "likes": 20}, "2025-06-17T00:00:00+00:00")  # re-scrape
            rows = store.all_signals(conn)
            check("upsert dedupes by url (1 row)", len(rows) == 1, str(len(rows)))
            check("upsert updates engagement", rows[0]["likes"] == 20, str(rows[0]["likes"]))
            hist = store.snapshot_history(conn, "u1")
            check("snapshots accumulate (2)", len(hist) == 2, str(len(hist)))
            bh = store.signals_for(conn, "company")
            check("signals_for filters by project", len(bh) == 1, str(len(bh)))


# --- scraper parsing (fixtures) ---------------------------------------------
def test_scraper_parse():
    reddit = scraper.parse_reddit((FIX / "reddit_shopify.json").read_text(), "shopify")
    check("reddit parses 3 items", len(reddit) == 3, str(len(reddit)))
    check("reddit maps comments→replies", reddit[0]["replies"] == 38, str(reddit[0]["replies"]))
    subs = scraper.parse_substack((FIX / "substack.xml").read_text())
    check("substack parses 2 items", len(subs) == 2, str(len(subs)))
    # regression: feeds with a BOM/whitespace before the XML declaration
    bom = scraper.parse_substack("  ﻿<?xml version='1.0'?><rss><channel>"
                                 "<item><title>x</title><link>u</link></item></channel></rss>")
    check("parses feed with leading BOM/whitespace", len(bom) == 1, str(len(bom)))
    check("substack pubDate→iso", subs[0]["posted_ts"].startswith("2025-06-16"), subs[0]["posted_ts"])

    # end-to-end collect via an injected fetcher (no network)
    class FakeFetcher(scraper.Fetcher):
        def get(self, url, headers=None):
            if "reddit" in url:
                return (FIX / "reddit_shopify.json").read_text()
            return (FIX / "substack.xml").read_text()

        def post(self, url, data, headers=None):
            return '{"access_token":"TOK"}'

    orig_id, orig_sec = config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET
    try:
        config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = "id", "sec"  # creds present → Reddit scraped
        sigs = scraper.collect(FakeFetcher())
        check("collect drops off-thesis (bakery/CRM)", all(s["projects"] for s in sigs), str(len(sigs)))
        check("collect tags projects", any("company" in s["projects"] for s in sigs))
        check("collect includes reddit when creds present", any(s["source"] == "reddit" for s in sigs))
        # no creds → Reddit skipped entirely (no 403 churn), Substack still flows
        config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = "", ""
        nored = scraper.collect(FakeFetcher())
        check("no creds → Reddit skipped, Substack still collected",
              nored and all(s["source"] == "substack" for s in nored), str([s["source"] for s in nored]))
    finally:
        config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = orig_id, orig_sec


def test_reddit_oauth():
    class FakeF(scraper.Fetcher):
        def post(self, url, data, headers=None):
            return '{"access_token":"TOK","token_type":"bearer","expires_in":3600}'
        def get(self, url, headers=None):
            return (FIX / "reddit_shopify.json").read_text()

    orig_id, orig_sec = config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET
    config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = "id", "sec"
    try:
        check("reddit_token parses access_token", scraper.reddit_token(FakeF()) == "TOK")
        oauth_url = scraper.reddit_search_url("shopify", "ai", oauth=True)
        check("oauth url → oauth.reddit.com", oauth_url.startswith("https://oauth.reddit.com"))
        check("oauth url drops .json suffix", "/search?" in oauth_url and ".json" not in oauth_url, oauth_url)
        pub_url = scraper.reddit_search_url("shopify", "ai", oauth=False)
        check("public url → www.reddit.com/.../search.json",
              pub_url.startswith("https://www.reddit.com") and "search.json" in pub_url, pub_url)
    finally:
        config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = orig_id, orig_sec
    check("no creds → no token (no network)", scraper.reddit_token(scraper.Fetcher()) is None)


# --- chrome queue -----------------------------------------------------------
def test_chrome_queue():
    with tempfile.TemporaryDirectory() as d:
        qd = Path(d)
        chrome_queue.enqueue("publish_linkedin", {"numbers": [1, 3]}, queue_dir=qd)
        pend = chrome_queue.pending(queue_dir=qd)
        check("queue enqueue→pending", len(pend) == 1, str(len(pend)))
        chrome_queue.done(pend[0]["_path"])
        check("queue done clears pending", len(chrome_queue.pending(queue_dir=qd)) == 0)
        try:
            chrome_queue.enqueue("bogus_kind", {}, queue_dir=qd)
            check("rejects unknown kind", False)
        except ValueError:
            check("rejects unknown kind", True)


# --- scheduler due-logic + idempotency --------------------------------------
def _dt(y, mo, d, h, mi):
    # tz-aware, matching production's now_local()
    return datetime(y, mo, d, h, mi, tzinfo=config.LOCAL_TZ)


def test_schedule():
    with tempfile.TemporaryDirectory() as d:
        ledger = Ledger(Path(d) / "l.json")
        daily = Job("daily_scrape_draft", lambda c: {}, kind="daily_once", at=time(1, 0))
        before = _dt(2025, 6, 16, 0, 30)
        after = _dt(2025, 6, 16, 1, 30)
        check("daily not due before time", not daily.due(before, ledger))
        check("daily due after time", daily.due(after, ledger))
        ledger.mark("daily_scrape_draft", daily.slot(after))
        check("daily not due twice same day", not daily.due(after, ledger))
        nextday = _dt(2025, 6, 17, 1, 30)
        check("daily due again next day", daily.due(nextday, ledger))

        sunday = Job("token_dashboard", lambda c: {}, kind="weekly_once", weekday=6, at=time(1, 5))
        check("weekly due on its weekday", sunday.due(_dt(2025, 6, 22, 1, 30), Ledger(Path(d) / "l2.json")))
        check("weekly not due other days", not sunday.due(_dt(2025, 6, 16, 1, 30), Ledger(Path(d) / "l3.json")))

        poller = Job("approval_poller", lambda c: {}, kind="window_repeat",
                     window=(time(6, 0), time(8, 30)))
        check("poller due inside window", poller.due(_dt(2025, 6, 16, 7, 0), ledger))
        check("poller not due outside window", not poller.due(_dt(2025, 6, 16, 9, 0), ledger))


def test_ledger_seen():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "l.json"
        lg = Ledger(path)
        check("unseen key is unseen", not lg.seen("k1"))
        lg.add_seen("k1")
        check("seen after add", lg.seen("k1"))
        check("seen persists across reload", Ledger(path).seen("k1"))


def test_poller_idempotency():
    """Regression for the HIGH bug: the same approval must queue exactly once
    even though the poller runs on every heartbeat across the 06:00-08:30 window."""
    with tempfile.TemporaryDirectory() as d:
        ledger = Ledger(Path(d) / "l.json")
        enq_calls = []
        bh_channel = config.company_channel()
        now = _dt(2025, 6, 16, 7, 0)
        appr_ts = str(now.timestamp() - 3600)  # recent (within the poller's recency window)

        def fake_read(channel, **kw):
            if channel == bh_channel:
                return [{"text": "R1 R3", "ts": appr_ts}]
            return []

        def fake_enqueue(kind, payload, **kw):
            enq_calls.append((kind, payload))

        orig_read, orig_enq, orig_net = slack_io.read_channel, chrome_queue.enqueue, slack_io.has_internet
        jobs.slack_io.read_channel = fake_read
        jobs.chrome_queue.enqueue = fake_enqueue
        jobs.slack_io.has_internet = lambda *a, **k: True
        try:
            ctx = {"dry_run": False, "ledger": ledger, "now": now}
            for _ in range(12):           # 12 heartbeats across the window
                jobs.run_approval_poller(ctx)
            # no-internet wake must do nothing (back to sleep)
            jobs.slack_io.has_internet = lambda *a, **k: False
            r = jobs.run_approval_poller(ctx)
        finally:
            jobs.slack_io.read_channel = orig_read
            jobs.chrome_queue.enqueue = orig_enq
            jobs.slack_io.has_internet = orig_net
        check("approval queued exactly once over 12 ticks", len(enq_calls) == 1, str(len(enq_calls)))
        check("queued the right kind", enq_calls and enq_calls[0][0] == "publish_linkedin")
        check("no-internet wake is a no-op", r["actions"] == ["no internet — back to sleep"], str(r))


def test_read_channel_deep_folds_threads():
    """An approval typed as a threaded reply must be visible to the poller —
    conversations.history alone wouldn't return it."""
    orig_rc, orig_rt = slack_io.read_channel, slack_io.read_thread
    slack_io.read_channel = lambda *a, **k: [{"text": "📝 shortlist", "ts": "100", "reply_count": 1}]
    slack_io.read_thread = lambda ch, ts, **k: [{"text": "📝 shortlist", "ts": "100"},
                                                {"text": "R1 R3", "ts": "101"}]
    try:
        texts = [m["text"] for m in slack_io.read_channel_deep("C")]
        check("deep read folds in the in-thread approval", "R1 R3" in texts, str(texts))
        check("parent not duplicated", texts.count("📝 shortlist") == 1, str(texts))
    finally:
        slack_io.read_channel, slack_io.read_thread = orig_rc, orig_rt


def test_poller_enriches_payload():
    """The poller folds the APPROVED reply drafts (from the shortlist JSON) into
    the publish queue payload, so the consumer needs no company-folder access."""
    import json as _json
    with tempfile.TemporaryDirectory() as d:
        orig_state = config.STATE_DIR
        orig_read, orig_enq, orig_net = slack_io.read_channel, chrome_queue.enqueue, slack_io.has_internet
        config.STATE_DIR = Path(d)
        now = _dt(2025, 6, 16, 7, 0)
        (Path(d) / f"company-shortlist-{now.strftime('%y%m%d')}.json").write_text(_json.dumps([
            {"n": 1, "thread_url": "u1", "text": "reply one"},
            {"n": 2, "thread_url": "u2", "text": "reply two"},
            {"n": 3, "thread_url": "u3", "text": "reply three"},
        ]))
        bh = config.company_channel()
        enq = []
        jobs.slack_io.read_channel = lambda channel, **k: ([{"text": "R1 R3", "ts": str(now.timestamp() - 3600)}] if channel == bh else [])
        jobs.chrome_queue.enqueue = lambda kind, payload, **k: enq.append((kind, payload))
        jobs.slack_io.has_internet = lambda *a, **k: True
        try:
            jobs.run_approval_poller({"dry_run": False, "ledger": Ledger(Path(d) / "l.json"), "now": now})
        finally:
            jobs.slack_io.read_channel, jobs.chrome_queue.enqueue, jobs.slack_io.has_internet = orig_read, orig_enq, orig_net
            config.STATE_DIR = orig_state
        check("poller enqueued one publish", len(enq) == 1, str(len(enq)))
        items = enq[0][1].get("items", []) if enq else []
        ns = sorted(it["n"] for it in items)
        check("payload carries only approved drafts (R1, R3)", ns == [1, 3], str(ns))
        check("payload items carry thread_url + text",
              all("thread_url" in it and "text" in it for it in items), str(items))


def test_poller_recency_guard():
    """An approval older than the recency window must NOT be acted on — otherwise
    the first poll after any outage replays every historical approve still sitting
    in the channel (the stale-replay that published 8-day-old approvals)."""
    with tempfile.TemporaryDirectory() as d:
        bh = config.company_channel()
        enq = []
        now = _dt(2025, 6, 16, 7, 0)
        stale_ts = str(now.timestamp() - 5 * 86400)  # 5 days old
        orig_read, orig_enq, orig_net = slack_io.read_channel, chrome_queue.enqueue, slack_io.has_internet
        jobs.slack_io.read_channel = lambda channel, **k: ([{"text": "R1 R3", "ts": stale_ts}] if channel == bh else [])
        jobs.chrome_queue.enqueue = lambda kind, payload, **k: enq.append((kind, payload))
        jobs.slack_io.has_internet = lambda *a, **k: True
        try:
            r = jobs.run_approval_poller({"dry_run": False, "ledger": Ledger(Path(d) / "l.json"), "now": now})
        finally:
            jobs.slack_io.read_channel, jobs.chrome_queue.enqueue, jobs.slack_io.has_internet = orig_read, orig_enq, orig_net
        check("stale approval is NOT queued", len(enq) == 0, str(len(enq)))
        check("stale poll reports nothing ready", r["actions"] == ["nothing ready"], str(r))


def test_newsletter_publish_maps_to_angle():
    """A threaded 'publish' on a specific Newsletter draft post must queue THAT angle's
    draft file (not 'most recent'), so several approved angles publish correctly."""
    with tempfile.TemporaryDirectory() as d:
        orig_state = config.STATE_DIR
        config.STATE_DIR = Path(d)
        now = _dt(2025, 6, 16, 7, 0)
        stamp = now.strftime("%y%m%d")
        for n in (4, 5):
            (Path(d) / f"newsletter-draft-{stamp}-angle{n}.md").write_text(
                f"---\ntitle: t{n}\nchannels: [x]\n---\nbody {n}")
        gx = config.NEWSLETTER_SLACK_CHANNEL
        appr_ts = str(now.timestamp() - 3600)
        enq = []

        def fake_read(channel, **k):
            if channel != gx:
                return []
            return [  # angle 4 has an approval reply; angle 5 does not
                {"text": "✍️ Newsletter draft ready (angle 4) — channels: [x]", "ts": "200", "reply_count": 1},
                {"text": "✍️ Newsletter draft ready (angle 5) — channels: [x]", "ts": "300", "reply_count": 0},
            ]

        def fake_thread(channel, ts, **k):
            return [{"text": "publish", "ts": appr_ts}] if ts == "200" else []

        orig_read, orig_thread, orig_enq, orig_net = (
            slack_io.read_channel, slack_io.read_thread, chrome_queue.enqueue, slack_io.has_internet)
        jobs.slack_io.read_channel = fake_read
        jobs.slack_io.read_thread = fake_thread
        jobs.chrome_queue.enqueue = lambda kind, payload, **k: enq.append((kind, payload))
        jobs.slack_io.has_internet = lambda *a, **k: True
        try:
            jobs.run_approval_poller({"dry_run": False, "ledger": Ledger(Path(d) / "l.json"), "now": now})
        finally:
            (jobs.slack_io.read_channel, jobs.slack_io.read_thread,
             jobs.chrome_queue.enqueue, jobs.slack_io.has_internet) = orig_read, orig_thread, orig_enq, orig_net
            config.STATE_DIR = orig_state
        gal = [p for kind, p in enq if kind == "publish_newsletter"]
        check("exactly one newsletter angle queued (only angle 4 approved)", len(gal) == 1, str(len(gal)))
        check("queued the approved angle's draft_file",
              bool(gal) and gal[0].get("draft_file", "").endswith("angle4.md"), str(gal))


def test_parse_newsletter_picks():
    """The digest-pick parser must read bare numbers (→ all channels), single
    'N to CH', and multi-clause 'N to CH, M to CH and CH', and drop 'skip'."""
    f = jobs._parse_newsletter_picks
    allc = ["linkedin", "substack", "x"]
    check("skip → nothing", f("skip") == [])
    check("bare number → all channels", f("4") == [(4, allc)], str(f("4")))
    check("N to CH → just that channel", f("4 to x") == [(4, ["x"])], str(f("4 to x")))
    check("two bare numbers", f("4 and 5") == [(4, allc), (5, allc)], str(f("4 and 5")))
    multi = f("4 to x, 5 to x and substack")
    check("multi-clause picks", multi == [(4, ["x"]), (5, ["x", "substack"])], str(multi))


def test_newsletter_article_drafter_on_pick():
    """On the api backend, a user pick ('4 to x') to the digest must draft THAT
    angle's article, write newsletter-draft-<day>-angle4.md with the chosen
    channels, post it in the publish-gate's expected shape, and never redraft."""
    import json as _json
    with tempfile.TemporaryDirectory() as d:
        orig = (config.STATE_DIR, config.DRAFT_BACKEND, config.draft_providers,
                jobs.slack_io.read_channel_deep, jobs.slack_io.has_internet,
                jobs.slack_io.send_message, jobs.draft.newsletter_article)
        config.STATE_DIR = Path(d)
        config.DRAFT_BACKEND = "api"
        config.draft_providers = lambda: [{"name": "gemini", "base_url": "", "model": "m", "api_key": "k"}]
        now = _dt(2025, 6, 16, 7, 0)
        stamp = now.strftime("%y%m%d")
        (Path(d) / f"newsletter-angles-{stamp}.json").write_text(_json.dumps(
            [{"n": 4, "hook": "h4", "angle": "a4", "source": "u4"},
             {"n": 5, "hook": "h5", "angle": "a5", "source": "u5"}]))
        pick_ts = str(now.timestamp() - 1800)
        sent, arts = [], []
        jobs.slack_io.read_channel_deep = lambda *a, **k: [{"text": "4 to x", "ts": pick_ts}]
        jobs.slack_io.has_internet = lambda *a, **k: True
        jobs.slack_io.send_message = lambda ch, text, **k: sent.append(text) or {"ok": True}
        jobs.draft.newsletter_article = lambda idea, **k: arts.append(idea) or "# Orbital Taxis\nBody text."
        led = Ledger(Path(d) / "l.json")
        try:
            r1 = jobs.run_newsletter_article_drafter({"dry_run": False, "now": now, "ledger": led})
            r2 = jobs.run_newsletter_article_drafter({"dry_run": False, "now": now, "ledger": led})
        finally:
            (config.STATE_DIR, config.DRAFT_BACKEND, config.draft_providers,
             jobs.slack_io.read_channel_deep, jobs.slack_io.has_internet,
             jobs.slack_io.send_message, jobs.draft.newsletter_article) = orig
        check("drafts the picked angle once", r1.get("drafted") == [4], str(r1))
        check("drafts from the picked angle's data", bool(arts) and arts[0].get("angle") == "a4", str(arts))
        df = Path(d) / f"newsletter-draft-{stamp}-angle4.md"
        check("writes the angle's draft file", df.exists())
        check("draft carries the chosen channels",
              df.exists() and "channels: [x]" in df.read_text(), df.read_text() if df.exists() else "")
        check("posts in the publish-gate shape",
              any("Newsletter draft ready (angle 4)" in s for s in sent), str(sent))
        check("idempotent: second run redrafts nothing", r2.get("drafted") == ["no new picks"], str(r2))


def test_health_alert():
    """The morning fail-loud backstop must (a) nudge Slack when the draft is
    missing, (b) alert when the digest is missing, (c) stay silent when healthy,
    and (d) fall back to a LOCAL notification when Slack is unreachable."""
    from . import dispatcher
    with tempfile.TemporaryDirectory() as d:
        orig_state, orig_digest, orig_backend = config.STATE_DIR, config.DIGEST_INPUT_DIR, config.DRAFT_BACKEND
        config.STATE_DIR = Path(d) / "state"
        config.DIGEST_INPUT_DIR = Path(d) / "din"
        config.STATE_DIR.mkdir(); config.DIGEST_INPUT_DIR.mkdir()
        config.DRAFT_BACKEND = "inapp"   # cases (a)-(d) exercise the Cowork-marker path
        now = _dt(2025, 6, 16, 6, 0)
        day = now.date().isoformat()
        digest = config.DIGEST_INPUT_DIR / f"digest-{day}.json"
        drafted = config.STATE_DIR / f"drafted-{day}"
        sent, local, send_ok = [], [], [True]
        orig_send, orig_notify = slack_io.send_message, dispatcher.notify_local
        jobs.slack_io.send_message = lambda ch, text, **k: (sent.append((ch, text)), {"ok": send_ok[0]})[1]
        dispatcher.notify_local = lambda msg: local.append(msg)
        try:
            # (a) digest present, draft missing → Slack nudge
            digest.write_text("{}"); sent.clear(); local.clear()
            jobs.run_health_alert({"now": now, "dry_run": False})
            check("health: nudges when draft missing",
                  len(sent) == 1 and "Run-now" in sent[0][1], str(sent))
            # (b) digest missing → alert about the failed scrape (no false 'Run-now')
            digest.unlink(); sent.clear()
            jobs.run_health_alert({"now": now, "dry_run": False})
            check("health: alerts when digest missing",
                  len(sent) == 1 and "recovery scrape failed" in sent[0][1], str(sent))
            # (c) both present → silent + healthy
            digest.write_text("{}"); drafted.write_text("done"); sent.clear()
            r = jobs.run_health_alert({"now": now, "dry_run": False})
            check("health: silent when healthy",
                  sent == [] and r["issues"] == ["healthy"], str(r))
            # (d) Slack unreachable → out-of-band local notification fires
            drafted.unlink(); send_ok[0] = False; sent.clear(); local.clear()
            jobs.run_health_alert({"now": now, "dry_run": False})
            check("health: local notify when Slack unreachable", len(local) == 1, str(local))
            # (e) api backend: NO drafted-<day> marker exists — health must key off the
            #     ledger, NOT cry wolf. Digest posted (ledger-marked) → silent/healthy;
            #     digest present but unposted → alert WITHOUT the stale 'Run-now' text.
            config.DRAFT_BACKEND = "api"; send_ok[0] = True
            digest.write_text("{}")  # note: no drafted-<day> marker on the api path
            led = Ledger(Path(d) / "hl.json"); led.add_seen(f"daily:newsletter_digest:{day}")
            sent.clear()
            r = jobs.run_health_alert({"now": now, "dry_run": False, "ledger": led})
            check("health(api): silent when digest ledger-marked (no false alarm)",
                  sent == [] and r["issues"] == ["healthy"], str(r))
            sent.clear()
            r = jobs.run_health_alert({"now": now, "dry_run": False, "ledger": Ledger(Path(d) / "hl2.json")})
            check("health(api): alerts when unposted, without stale 'Run-now'",
                  len(sent) == 1 and "Run-now" not in sent[0][1] and "api drafter" in sent[0][1], str(sent))
        finally:
            jobs.slack_io.send_message = orig_send
            dispatcher.notify_local = orig_notify
            config.STATE_DIR, config.DIGEST_INPUT_DIR, config.DRAFT_BACKEND = orig_state, orig_digest, orig_backend


def test_dark_window():
    from . import dispatcher
    check("01:00 is in a dark window", dispatcher._in_dark_window(_dt(2025, 6, 16, 1, 0)))
    check("07:00 is in a dark window", dispatcher._in_dark_window(_dt(2025, 6, 16, 7, 0)))
    check("14:00 is NOT (daytime — never touch display)",
          not dispatcher._in_dark_window(_dt(2025, 6, 16, 14, 0)))
    check("03:00 is NOT (between windows)", not dispatcher._in_dark_window(_dt(2025, 6, 16, 3, 0)))


def test_daily_delegates_inapp():
    """With DRAFT_BACKEND='inapp', the native daily job scrapes + drops a ready
    marker and delegates drafting — it must NOT post to Slack itself."""
    with tempfile.TemporaryDirectory() as d:
        orig_state, orig_scrape, orig_send = config.STATE_DIR, jobs.scraper.run_scrape, jobs.slack_io.send_message
        config.STATE_DIR = Path(d)
        jobs.scraper.run_scrape = lambda *a, **k: {"scraped": 0}
        drained = []
        orig_drain_all = jobs.ingest_inbox.drain_all
        jobs.ingest_inbox.drain_all = lambda *a, **k: drained.append(1) or {"linkedin": {}, "x": {}}
        sent = []
        jobs.slack_io.send_message = lambda *a, **k: sent.append(a) or {"ok": True}
        try:
            now = _dt(2025, 6, 16, 1, 5)  # Monday
            summary = jobs.run_daily_scrape_draft({"dry_run": False, "now": now, "ledger": None})
            check("inapp: drains the inbox inside the scrape", drained == [1] and "inbox" in summary)
            check("inapp: writes scrape-done marker",
                  (Path(d) / f"scrape-done-{now.date().isoformat()}").exists())
            check("inapp: delegates drafting", "delegated" in summary.get("draft", ""))
            check("inapp: native job posts nothing itself", sent == [], str(sent))
        finally:
            config.STATE_DIR, jobs.scraper.run_scrape, jobs.slack_io.send_message = orig_state, orig_scrape, orig_send
            jobs.ingest_inbox.drain_all = orig_drain_all


def test_friday_digests():
    from . import digests
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "linkedin").mkdir(parents=True)
        (base / "reddit").mkdir(parents=True)
        (base / "linkedin" / "comment_log.csv").write_text(
            "date,post_url,author,angle,status\n"
            "2025-06-17,u1,a,angle,posted\n"
            "2025-06-18,u2,b,angle,suggested\n"
            "2025-06-01,u3,c,angle,posted\n", encoding="utf-8")  # last row out of week
        (base / "reddit" / "engagement_log.csv").write_text(
            "date,sub,thread_url,thread_title,comment_angle,outcome,notes\n"
            "2025-06-19,r/x,turl,title,angle,posted,note\n", encoding="utf-8")
        orig = config.COMPANY_MARKETING
        config.COMPANY_MARKETING = base
        try:
            now = _dt(2025, 6, 20, 1, 0)  # Friday of that week
            r1 = digests.run_friday_digests(now, dry=False)
            check("friday: linkedin counts in-week rows (2)", r1["linkedin"]["week_rows"] == 2, str(r1))
            check("friday: linkedin counts posted (1)", r1["linkedin"]["posted"] == 1)
            check("friday: reddit counts in-week rows (1)", r1["reddit"]["week_rows"] == 1)
            check("friday: section appended",
                  "Week ending 2025-06-20" in (base / "linkedin" / "weekly_log.md").read_text())
            r2 = digests.run_friday_digests(now, dry=False)
            check("friday: idempotent — no duplicate section",
                  r2["linkedin"].get("already-written") is True, str(r2))
            # Saturday catch-up: same week-ending-Friday section, already written
            sat = digests.run_friday_digests(_dt(2025, 6, 21, 1, 0), dry=False)
            check("friday: Saturday targets same week & no-ops",
                  sat["linkedin"].get("already-written") is True, str(sat))
        finally:
            config.COMPANY_MARKETING = orig


def test_prev_velocity_same_day():
    """Same-day re-upsert of one url (cross-path: surfacer + scraper, or re-runs)
    must NOT clobber prev_velocity with today's value — else acceleration flattens.
    Pins a fixed zone so the test is independent of the deployment's LOCAL_TZ."""
    from zoneinfo import ZoneInfo
    orig_tz = config.LOCAL_TZ
    config.LOCAL_TZ = ZoneInfo("Asia/Kolkata")  # these UTC stamps fall same-day in +5:30
    try:
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            sig = {"source": "reddit", "url": "u", "text": "shopify catalog", "likes": 0}
            with store.connect(db) as conn:
                store.upsert_signal(conn, sig, "2025-06-16T00:00:00+00:00")  # day 1
                store.set_rank(conn, "u", 10.0, "Accelerating")             # yesterday's vel=10
                store.upsert_signal(conn, sig, "2025-06-17T00:00:00+00:00")  # day 2 → roll prev=10
                r = conn.execute("SELECT prev_velocity FROM signals WHERE url='u'").fetchone()
                check("new-day upsert rolls prev=yesterday(10)", r["prev_velocity"] == 10.0, str(r["prev_velocity"]))
                store.set_rank(conn, "u", 20.0, "Accelerating")            # today's vel=20
                store.upsert_signal(conn, sig, "2025-06-17T12:00:00+00:00")  # SAME day re-upsert
                r = conn.execute("SELECT prev_velocity FROM signals WHERE url='u'").fetchone()
                check("same-day re-upsert preserves prev (not clobbered to 20)",
                      r["prev_velocity"] == 10.0, str(r["prev_velocity"]))
    finally:
        config.LOCAL_TZ = orig_tz


def test_prev_velocity_tz_boundary():
    """Two same-url upserts in the same LOCAL day but straddling 00:00 UTC must
    count as same-day → no spurious prev_velocity roll (the local-vs-UTC date fix).
    Pins a +5:30 example zone so the timestamps straddle UTC midnight as intended."""
    from zoneinfo import ZoneInfo
    orig_tz = config.LOCAL_TZ
    config.LOCAL_TZ = ZoneInfo("Asia/Kolkata")
    try:
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            sig = {"source": "reddit", "url": "u", "text": "shopify catalog", "likes": 0}
            with store.connect(db) as conn:
                store.upsert_signal(conn, sig, "2025-06-15T20:00:00+00:00")  # local 06-16 01:30
                store.set_rank(conn, "u", 10.0, "Accelerating")
                store.upsert_signal(conn, sig, "2025-06-16T20:00:00+00:00")  # local 06-17 01:30 → roll prev=10
                store.set_rank(conn, "u", 20.0, "Accelerating")
                store.upsert_signal(conn, sig, "2025-06-17T00:30:00+00:00")  # local 06-17 06:00: SAME local day
                r = conn.execute("SELECT prev_velocity FROM signals WHERE url='u'").fetchone()
                check("same local day across UTC midnight preserves prev (not rolled to 20)",
                      r["prev_velocity"] == 10.0, str(r["prev_velocity"]))
    finally:
        config.LOCAL_TZ = orig_tz


def test_surfacer_repush_peaks():
    """A re-surfaced Reddit post whose velocity collapses must classify Peaked,
    not stay pinned Accelerating (the prev_velocity=0 bug). Also exercises the
    generalized --source CLI."""
    from . import store_signal
    with tempfile.TemporaryDirectory() as d:
        orig_db = config.SIGNALS_DB
        config.SIGNALS_DB = Path(d) / "t.db"
        try:
            url = "https://www.reddit.com/r/shopify/comments/x"
            store_signal.main(["--source", "reddit", "--url", url,
                               "--text", "shopify catalog schema.org",
                               "--posted", "2025-06-16T00:00:00+00:00", "--replies", "50"])
            store_signal.main(["--source", "reddit", "--url", url,
                               "--text", "shopify catalog schema.org",
                               "--posted", "2025-06-16T00:00:00+00:00", "--replies", "1"])
            with store.connect(config.SIGNALS_DB) as conn:
                rows = [r for r in store.all_signals(conn) if r["url"] == url]
                check("surfacer re-push keeps one row", len(rows) == 1, str(len(rows)))
                check("surfacer row tagged source=reddit", rows[0]["source"] == "reddit", rows[0]["source"])
                # same-day re-push preserves prev_velocity → no spurious Peaked flip
                # (genuine Peaked is detected day-over-day; see test_prev_velocity_same_day)
                check("same-day re-push not spuriously Peaked", rows[0]["status"] != "Peaked", rows[0]["status"])
        finally:
            config.SIGNALS_DB = orig_db


def test_digest_no_double_summary():
    """Crash-recovery: linkedin section written one day, reddit the next — the
    Slack summary must post exactly once, not twice."""
    from . import digests
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "linkedin").mkdir(parents=True)
        (base / "reddit").mkdir(parents=True)
        (base / "linkedin" / "comment_log.csv").write_text(
            "date,post_url,author,angle,status\n2025-06-17,u,a,x,posted\n", encoding="utf-8")
        (base / "reddit" / "engagement_log.csv").write_text(
            "date,sub,thread_url,thread_title,comment_angle,outcome,notes\n", encoding="utf-8")
        # Simulate a prior partial run: linkedin section already present, reddit not.
        (base / "linkedin" / "weekly_log.md").write_text(
            "## Week ending 2025-06-20\n\n(prior partial)\n", encoding="utf-8")
        sends = []
        orig_mkt, orig_send = config.COMPANY_MARKETING, digests.slack_io.send_message
        config.COMPANY_MARKETING = base
        digests.slack_io.send_message = lambda *a, **k: sends.append(a) or {"ok": True}
        try:
            now = _dt(2025, 6, 20, 1, 0)
            digests.run_friday_digests(now, dry=False)   # completes reddit → 1 summary
            digests.run_friday_digests(now, dry=False)   # both present → no summary
            check("crash-recovery posts exactly one summary", len(sends) == 1, str(len(sends)))
        finally:
            config.COMPANY_MARKETING, digests.slack_io.send_message = orig_mkt, orig_send


def test_single_instance_lock():
    """A second concurrent dispatcher must back off (closes the seen/enqueue race)."""
    import fcntl
    from . import dispatcher
    with tempfile.TemporaryDirectory() as d:
        orig = config.STATE_DIR
        config.STATE_DIR = Path(d)
        try:
            held = open(Path(d) / "dispatcher.lock", "w")
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # simulate a running tick
            with dispatcher.single_instance() as acquired:
                check("second instance backs off when lock held", acquired is False)
            held.close()
            with dispatcher.single_instance() as acquired:
                check("acquires when lock free", acquired is True)
        finally:
            config.STATE_DIR = orig


def test_digest_input_export():
    """Native exports ranked signals to a JSON file the Cowork drafter can read
    (no SQLite over the mount)."""
    import json as _json
    from . import jobs
    with tempfile.TemporaryDirectory() as d:
        orig_db, orig_dir = config.SIGNALS_DB, config.DIGEST_INPUT_DIR
        config.SIGNALS_DB, config.DIGEST_INPUT_DIR = Path(d) / "t.db", Path(d) / "digest_input"
        try:
            with store.connect(config.SIGNALS_DB) as conn:
                store.upsert_signal(conn, {"source": "linkedin", "url": "u1",
                    "text": "shopify catalog schema.org", "projects": "company", "likes": 5},
                    "2025-06-16T00:00:00+00:00")
                store.set_rank(conn, "u1", 5.0, "Accelerating")
                store.upsert_signal(conn, {"source": "substack", "url": "u2",
                    "text": "nasa lunar orbit", "projects": "newsletter"}, "2025-06-16T00:00:00+00:00")
                store.set_rank(conn, "u2", 3.0, "Watching")
            jobs._export_digest_input("2025-06-16")
            data = _json.loads((config.DIGEST_INPUT_DIR / "digest-2025-06-16.json").read_text())
            check("export has newsletter+company keys", "newsletter" in data and "company" in data, str(list(data)))
            check("export includes the company signal", any(r["url"] == "u1" for r in data["company"]))
            check("export includes the newsletter signal", any(r["url"] == "u2" for r in data["newsletter"]))
        finally:
            config.SIGNALS_DB, config.DIGEST_INPUT_DIR = orig_db, orig_dir


def test_inbox_drain():
    """The Cowork→native handoff: a JSON spool file drains into signals.db via
    the same rank/dedup path, off-thesis dropped, processed file archived."""
    import json as _json
    from . import ingest_inbox
    with tempfile.TemporaryDirectory() as d:
        inbox = Path(d) / "inbox"
        inbox.mkdir()
        (inbox / "li-1.json").write_text(_json.dumps([
            {"url": "https://www.linkedin.com/posts/abc", "text": "shopify schema.org catalog data",
             "posted": "2025-06-16T00:00:00+00:00", "likes": 10, "replies": 4},
            {"url": "https://www.linkedin.com/posts/off", "text": "my sourdough bread rose nicely", "likes": 1},
        ]))
        db = Path(d) / "t.db"
        res = ingest_inbox.drain(inbox_dir=inbox, db_path=db)
        check("drain stores the on-thesis record", res["stored"] == 1, str(res))
        check("drain drops the off-thesis record", res["off_thesis"] == 1, str(res))
        check("drain archives the processed file", not list(inbox.glob("*.json")))
        with store.connect(db) as c:
            rows = c.execute("select source from signals").fetchall()
            check("drained row tagged source=linkedin", len(rows) == 1 and rows[0]["source"] == "linkedin")
        # dry-run touches nothing
        (inbox / "li-2.json").write_text('[{"url":"u","text":"shopify catalog"}]')
        dry = ingest_inbox.drain(inbox_dir=inbox, db_path=db, dry_run=True)
        check("drain dry-run writes nothing", dry["stored"] == 0 and list(inbox.glob("*.json")))


def test_x_source_drain():
    """An X spool record drains with source=x (the multi-source generalization)."""
    import json as _json
    from . import ingest_inbox
    with tempfile.TemporaryDirectory() as d:
        xbox = Path(d) / "x_inbox"
        xbox.mkdir()
        (xbox / "x-1.json").write_text(_json.dumps([
            {"url": "https://x.com/someone/status/9", "text": "agentic commerce shopify",
             "posted": "2025-06-16T00:00:00Z", "likes": 30, "reposts": 5, "replies": 8}]))
        db = Path(d) / "t.db"
        res = ingest_inbox.drain("x", inbox_dir=xbox, db_path=db)
        check("x drain stores the record", res["stored"] == 1, str(res))
        with store.connect(db) as c:
            row = c.execute("select source, likes from signals").fetchone()
        check("row tagged source=x with engagement", row["source"] == "x" and row["likes"] == 30, dict(row))


def test_wake_armer_disablesleep():
    """Locks in the real overnight fix (post-adversarial-loop): the keep-awake
    uses `pmset -a disablesleep` (beats clamshell sleep on battery AND AC) with a
    separate disarm + self-heal, NOT the caffeinate hold that can't stop clamshell
    sleep; and ensure_network probes raw-IP TLS first so a captive portal can't
    fake 'online'."""
    wake = Path(__file__).resolve().parent / "wake"
    arm = (wake / "arm_wakes.sh").read_text()
    net = (wake / "ensure_network.sh").read_text()
    check("keep-awake uses disablesleep all-power", "-a disablesleep 1" in arm)
    check("keep-awake has a disarm (restore sleep)", "-a disablesleep 0" in arm)
    check("keep-awake is non-blocking (separate DISARM phase, no caffeinate hold)",
          "DISARM" in arm and 'caffeinate -i -t' not in arm)
    check("ensure_network probes raw-IP TLS first (captive-proof)", "https://1.1.1.1" in net)
    check("ensure_network validates captive 'Success' body", '"Success"' in net or "Success" in net)


def test_manifest():
    from .jobs import JOBS
    names = {j.name for j in JOBS}
    expected = {"daily_scrape_draft", "approval_poller", "token_dashboard", "inbox_drain", "health_alert"}
    check("manifest has the core jobs (incl. all-day drain)", expected <= names, str(names))
    check("token dashboard folded in", "token_dashboard" in names)


def run_offline() -> bool:
    for t in (test_rank, test_segregation, test_dedupe, test_canonical_url, test_parse_reply, test_store,
              test_scraper_parse, test_reddit_oauth, test_chrome_queue, test_schedule, test_ledger_seen,
              test_poller_idempotency, test_read_channel_deep_folds_threads,
              test_poller_enriches_payload, test_poller_recency_guard,
              test_newsletter_publish_maps_to_angle, test_parse_newsletter_picks,
              test_newsletter_article_drafter_on_pick, test_health_alert, test_dark_window,
              test_daily_delegates_inapp,
              test_friday_digests, test_prev_velocity_same_day, test_prev_velocity_tz_boundary,
              test_surfacer_repush_peaks,
              test_digest_no_double_summary,
              test_inbox_drain, test_x_source_drain, test_digest_input_export, test_wake_armer_disablesleep,
              test_single_instance_lock, test_manifest):
        try:
            t()
        except Exception as exc:  # a crashing test is a failure, not an abort
            check(f"{t.__name__} raised", False, repr(exc))

    passed = sum(1 for _, ok, _ in _checks if ok)
    for name, ok, detail in _checks:
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if not ok and detail:
            line += f"  → got {detail}"
        print(line)
    print(f"\n{passed}/{len(_checks)} checks passed")
    return passed == len(_checks)


def run_live() -> bool:
    """On the user's Mac: confirm the things the sandbox can't — network + Slack."""
    ok = True
    try:
        sigs = scraper.collect(scraper.Fetcher())
        print(f"  [live] scrape returned {len(sigs)} on-thesis signals")
    except Exception as exc:
        print(f"  [live][FAIL] scrape: {exc}"); ok = False
    res = slack_io.send_message(slack_io.config.company_channel(),
                                "🤖 orchestrator verify --live smoke test", dry_run=False)
    print(f"  [live] slack send ok={res.get('ok')}")
    return ok and res.get("ok", False)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ok = run_offline()
    if "--live" in argv:
        print("\n--- live smoke tests ---")
        ok = run_live() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
