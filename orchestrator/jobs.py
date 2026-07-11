"""
Job functions wired to the cron manifest.

Three jobs replace the 9 retired Cowork tasks:

  token_dashboard   (Sun 01:05)  → existing dashboard.py --once, unchanged
  daily_scrape_draft(daily 01:00)→ scrape → rank → draft (Claude API) → Slack;
                                    on Tue also the weekly Company post; on Fri
                                    also the Reddit + LinkedIn weekly digests
  approval_poller   (06:00–08:30)→ read Slack, parse approvals, hold/skip inline,
                                    queue publishes for the in-app Chrome task,
                                    bounce edits back through draft.py

Only daily_scrape_draft's drafting and the queued publishes touch Claude.
"""
from __future__ import annotations

import subprocess
import json
import re
from datetime import datetime, time

from . import config, digests, draft, scraper, slack_io, store, chrome_queue, ingest_inbox
from .schedule import Job


def _approved_shortlist_items(numbers, now) -> list[dict]:
    """Load the R-numbered reply drafts the drafter wrote to
    orchestrator/state/company-shortlist-<stamp>.json, filtered to the approved
    numbers, so the publish queue carries the TEXT itself — no dependency on the
    company folder / comment_log.csv. Falls back to the most recent shortlist if
    today's is absent (e.g. an approval that lands the next morning)."""
    want = set(numbers)
    f = config.STATE_DIR / f"company-shortlist-{now.strftime('%y%m%d')}.json"
    if not f.exists():
        cands = sorted(config.STATE_DIR.glob("company-shortlist-*.json"))
        if not cands:
            return []
        f = cands[-1]
    try:
        items = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [it for it in items if isinstance(it, dict) and it.get("n") in want]


def _format_newsletter_digest(angles: list[dict]) -> str:
    """Render structured angles (from draft.newsletter_angles) into the Slack
    digest. Leads with 🌙 (a bot marker) so the poller never mistakes it for a
    pick."""
    lines = ["🌙 *Digest*", ""]
    for a in angles:
        lines.append(f"*{a.get('n')}.* {a.get('hook', '')}")
        if a.get("angle"):
            lines.append(f"    ↳ {a['angle']}")
        if a.get("source"):
            lines.append(f"    {a['source']}")
        lines.append("")
    lines.append("Reply a number to draft (defaults to all channels — add 'to x' / 'to substack' / "
                 "'to linkedin' to limit, e.g. `3 to substack`), or 'skip' to pass.")
    return "\n".join(lines)


def _parse_newsletter_picks(text: str) -> list[tuple[int, list[str]]]:
    """Parse a digest pick reply → [(angle_n, [channels])]. Handles '4', '4 5',
    '4 to x', '4 to x, 5 to x and substack', 'skip'. Bare number → all channels."""
    t = (text or "").strip().lower()
    if not t or t in ("skip", "pass"):
        return []
    out = []
    for clause in re.split(r"[,;]|\band\b(?=\s*\d)", t):
        m = re.match(r"\s*(\d+)\s*(?:to\s+(.+))?$", clause.strip())
        if not m:
            continue
        spec = m.group(2) or ""
        chans = [c for c in ("x", "substack", "linkedin") if c in spec]
        out.append((int(m.group(1)), chans or ["linkedin", "substack", "x"]))
    return out


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        clean = line.strip().lstrip("#* ").strip()
        if clean:
            return clean[:120]
    return "Untitled"


# --- job: token usage dashboard (deterministic; subprocess) -----------------
def run_token_dashboard(ctx: dict) -> dict:
    if ctx.get("dry_run"):
        return {"job": "token_dashboard", "status": "dry-run (would run dashboard.py --once)"}
    proc = subprocess.run(
        [str(config.DASHBOARD_PYTHON), str(config.DASHBOARD_PY),
         "--once", "--config", str(config.DASHBOARD_CONFIG)],
        capture_output=True, text=True, timeout=600,
    )
    return {"job": "token_dashboard", "rc": proc.returncode,
            "tail": proc.stdout.strip()[-300:]}


def _export_digest_input(day: str) -> None:
    """Write today's ranked signals to digest_input/digest-<day>.json so the
    in-app (Cowork) drafter reads a FILE, not signals.db (SQLite fails over its
    mount). Same handoff trick as the surfacer spool, in reverse."""
    import json
    with store.connect(config.SIGNALS_DB) as conn:
        data = {
            "date": day,
            "newsletter": store.signals_for(conn, "newsletter")[:12],
            "company": store.signals_for(conn, "company", statuses=["Accelerating", "Watching"])[:10],
        }
    config.DIGEST_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    (config.DIGEST_INPUT_DIR / f"digest-{day}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8")


# --- job: daily scrape + draft + deliver ------------------------------------
def run_daily_scrape_draft(ctx: dict) -> dict:
    dry = ctx.get("dry_run", False)
    now = ctx["now"]
    ledger = ctx.get("ledger")
    day = now.date().isoformat()
    summary = {"job": "daily_scrape_draft", "posted": []}

    # 1) Drain the LinkedIn surfacer's spool (also runs every heartbeat via its
    #    own job, so a surfacer file that lands LATE — the common case, since the
    #    Cowork surfacer defers to the next wake when the Mac was asleep — is
    #    still ingested within ~12 min and ranked by its own set_rank). Doing it
    #    here too means whatever is already spooled is in the store before the
    #    rank-all pass below.
    summary["inbox"] = ingest_inbox.drain_all(dry_run=dry)
    summary["scrape"] = {"skipped": "dry-run"} if dry else scraper.run_scrape()

    # Export the ranked signals to a plain JSON file the in-app (Cowork) drafter
    # can READ (it can't open signals.db over its FUSE mount; file reads work).
    # Then drop the ready-marker.
    if not dry:
        _export_digest_input(day)
        (config.STATE_DIR / f"scrape-done-{day}").write_text(
            datetime.now().astimezone().isoformat(), encoding="utf-8")

    # 2) Friday weekly digests — deterministic native rollups (no Claude),
    #    independent of the drafting backend. Idempotent per week. Triggered
    #    Fri/Sat/Sun (not just Friday) so a Mac asleep through Friday still
    #    catches up on the weekend; week_bounds targets the just-ended Mon–Fri
    #    and _has_section prevents duplicate writes.
    if now.weekday() >= 4:
        summary["friday_digests"] = digests.run_friday_digests(now, dry=dry)

    # 3) DRAFTING. Default: delegated to the in-app Cowork task (subscription,
    #    no API billing). The native job stops here. The "api" fallback below
    #    drafts + posts inline via the Anthropic API.
    if config.DRAFT_BACKEND != "api":
        summary["draft"] = "delegated to in-app draft-generator (subscription)"
        return summary

    def post_once(step: str, channel: str, text: str) -> None:
        """Deliver a Slack message at most once per day, so a job retried after
        a mid-run failure (it isn't ledger-marked until success) never re-posts
        a digest it already sent."""
        key = f"daily:{step}:{day}"
        # Safety: with no drafting provider configured draft.py returns a stub —
        # never post that placeholder to a real Slack channel. (Loading the agent
        # before any key is set is therefore harmless.)
        if not dry and not config.draft_providers():
            summary["posted"].append(f"{step}:skipped-no-api-key")
            return
        if ledger and not dry and ledger.seen(key):
            summary["posted"].append(f"{step}:already-sent")
            return
        slack_io.send_message(channel, text, dry_run=dry)
        if ledger and not dry:
            ledger.add_seen(key)
        summary["posted"].append(step)

    # read ranked signals back, per project
    with store.connect(config.SIGNALS_DB) as conn:
        gx = store.signals_for(conn, "newsletter")
        bh = store.signals_for(conn, "company", statuses=["Accelerating", "Watching"])

    # 3) draft (LLM API) + deliver to Slack — each step idempotent per day.
    # Newsletter: draft STRUCTURED angles and persist them, so the headless
    # article-drafter can later map a pick number ("4 to x") back to its angle.
    # Post the formatted digest; if the model didn't return parseable JSON, fall
    # back to the free-text digest (article-on-pick just won't be available then).
    angles = draft.newsletter_angles(gx, dry_run=dry)
    if angles:
        if not dry:
            (config.STATE_DIR / f"newsletter-angles-{now.strftime('%y%m%d')}.json").write_text(
                json.dumps(angles, ensure_ascii=False), encoding="utf-8")
        post_once("newsletter_digest", config.NEWSLETTER_SLACK_CHANNEL, _format_newsletter_digest(angles))
    else:
        post_once("newsletter_digest", config.NEWSLETTER_SLACK_CHANNEL, draft.newsletter_digest(gx, dry_run=dry))
    post_once("company_shortlist", config.company_channel(), draft.company_shortlist(bh, dry_run=dry))

    if now.weekday() == 1:  # Tuesday: one original weekly post draft
        post_once("company_weekly_post", config.company_channel(),
                  "📝 Weekly post draft\n\n" + draft.company_weekly_post(bh, dry_run=dry))

    # (Friday digests already ran above, before the backend branch.)
    return summary


# --- job: approval poller (deterministic; queues Chrome work) ---------------
def run_approval_poller(ctx: dict) -> dict:
    dry = ctx.get("dry_run", False)
    ledger = ctx.get("ledger")
    found: list[str] = []

    # Window B polling gate: if this dark-wake has no internet, do nothing and
    # let the Mac idle-sleep until the next 30-min wake.
    if not dry and not slack_io.has_internet():
        return {"job": "approval_poller", "actions": ["no internet — back to sleep"]}

    # Recency guard: only act on approvals from the recent window. Without it, the
    # FIRST successful poll after any outage (e.g. the bot regaining channel access)
    # replays EVERY historical approve/publish still in the channel history —
    # publishing content approved days ago. 36h covers an overnight digest (~01:35)
    # → its morning approval window, plus a full day of slack, while excluding the
    # multi-day-old messages that caused the stale-replay.
    now = ctx.get("now")
    cutoff = now.timestamp() - 36 * 3600 if now else 0.0

    def _recent(ts: str) -> bool:
        try:
            return float(ts) >= cutoff
        except (TypeError, ValueError):
            return False

    def act_once(channel_tag: str, ts: str, queue_kind: str, payload: dict, label: str) -> None:
        """Queue a publish at most once per approving message. Without this the
        06:00-08:30 poller would re-queue the SAME approval on every ~12-min
        heartbeat (~12x), causing duplicate public posts."""
        key = f"approval:{channel_tag}:{ts}"
        if ledger and ledger.seen(key):
            return
        if not dry:
            chrome_queue.enqueue(queue_kind, payload)
            if ledger:
                ledger.add_seen(key)
        found.append(label)

    # Company: read its channel, find approvals, queue publishes for Chrome task.
    for m in slack_io.read_channel_deep(config.company_channel(), dry_run=dry):
        txt, ts = m.get("text", ""), m.get("ts", "")
        if slack_io.is_bot_message(txt) or not ts or not _recent(ts):
            continue
        intent = slack_io.parse_reply(txt, "awaiting_approval")
        if intent[0] == "approve":
            items = _approved_shortlist_items(intent[1], ctx["now"])  # carry the TEXT in the queue
            act_once("company", ts, "publish_linkedin",
                     {"numbers": intent[1], "reply_ts": ts, "items": items},
                     f"company approve {intent[1]} ({len(items)} drafts)")
        elif intent[0] == "skip":
            found.append("company skip")
        # 'edit'/'hold' route through the same seen-keyed machinery as needed.

    # Newsletter: publish gate. An approval is a threaded "publish" reply on a
    # specific draft post ("✍️ Newsletter draft ready (angle N) — channels: ..."), so
    # map each approved thread back to ITS angle's draft file. A flat scan can't
    # tell angle 4's "publish" from angle 5's — it would publish the wrong (or the
    # same) article. Keying the queue per-angle also dedups the split (1/2, 2/2)
    # draft posts, each of which may carry its own "publish" reply.
    _angle_re = re.compile(r"angle\s*(\d+)", re.I)
    for parent in slack_io.read_channel(config.NEWSLETTER_SLACK_CHANNEL, dry_run=dry):
        ptext, pts = parent.get("text", ""), parent.get("ts", "")
        am = _angle_re.search(ptext)
        if not (pts and am and slack_io.is_bot_message(ptext)):
            continue  # only OUR draft posts that name an angle
        if int(parent.get("reply_count", 0) or 0) <= 0:
            continue  # no replies → no approval yet
        angle = am.group(1)
        approved = any(
            not slack_io.is_bot_message(r.get("text", ""))
            and _recent(r.get("ts", ""))
            and slack_io.parse_reply(r.get("text", ""), "awaiting_approval")[0] == "publish"
            for r in slack_io.read_thread(config.NEWSLETTER_SLACK_CHANNEL, pts, dry_run=dry)
        )
        if not approved:
            continue
        drafts = sorted(config.STATE_DIR.glob(f"newsletter-draft-*-angle{angle}.md"))
        if not drafts:
            continue  # approved but not drafted — nothing to publish
        act_once("newsletter", f"angle{angle}", "publish_newsletter",
                 {"draft_file": str(drafts[-1]), "angle": angle, "reply_ts": pts},
                 f"newsletter publish angle {angle}")

    return {"job": "approval_poller", "actions": found or ["nothing ready"]}


# --- job: drain the LinkedIn surfacer spool into signals.db -----------------
# Runs EVERY heartbeat (all day) so a surfacer file dropped at ANY time — incl.
# when the Cowork surfacer deferred to a morning wake — is ingested within ~12
# min and ranked by its own set_rank, instead of waiting for the next night's
# scrape. (Empty-spool ticks are a ~0.03 ms directory glob.) The 01:00 scrape
# also drains, so anything already spooled is in the store before its rank-all.
def run_inbox_drain(ctx: dict) -> dict:
    return ingest_inbox.drain_all(dry_run=ctx.get("dry_run", False))


# --- the manifest -----------------------------------------------------------
# Order matters within a tick: the time-sensitive daily scrape+draft runs first
# so that on Sundays a slow (up to 600s) token-dashboard subprocess can't delay
# the 01:00 Slack drafts. The inbox drain runs last — cheap, must never delay.
# --- job: morning health alert (fail-loud backstop) -------------------------
def run_health_alert(ctx: dict) -> dict:
    """Fail-loud morning backstop — guarantees the overnight pipeline never fails
    SILENTLY. It runs at the first Window-B wake, AFTER the manifest has already
    re-run `daily_scrape_draft` this tick (so a missed overnight scrape is
    recovered before we check). Then:
      • digest still missing → the recovery scrape also failed (Wi-Fi/dispatcher);
      • digest present, no `drafted-<day>` marker → the in-app drafter hasn't
        posted (it can't run while the lid is shut) → nudge the user to Run-now.
    Alerts via Slack, and ALSO via a local macOS notification when Slack is
    unreachable — so a total outage (the exact case the backstop exists for) is
    never silent."""
    now = ctx["now"]
    dry = ctx.get("dry_run", False)
    ledger = ctx.get("ledger")
    day = now.date().isoformat()
    digest = config.DIGEST_INPUT_DIR / f"digest-{day}.json"
    issues: list[str] = []

    if not digest.exists():
        issues.append("overnight scrape did not produce today's digest, and the 06:00 "
                      "recovery scrape failed too — check Wi-Fi and the dispatcher")
    elif config.DRAFT_BACKEND == "api":
        # Headless api path: "posted" is signalled by daily_scrape_draft's
        # post_once ledger-marking `daily:newsletter_digest:<day>` — there is NO
        # Cowork `drafted-<day>` marker (that task is retired on this backend), so
        # checking for it here would false-alarm every morning on a perfect run.
        if not (ledger and ledger.seen(f"daily:newsletter_digest:{day}")):
            issues.append("digest data is ready but the api drafter didn't post it — "
                          "check the drafting provider key and the run log")
    elif not (config.STATE_DIR / f"drafted-{day}").exists():
        # in-app path: the Cowork draft-generator writes `drafted-<day>` on success.
        issues.append("digest data is ready but no draft was posted — Run-now the "
                      "draft-generator Cowork task to send today's digest + shortlist")

    if issues and not dry:
        text = f"⚠️ Overnight pipeline health ({day}):\n• " + "\n• ".join(issues)
        res = slack_io.send_message(config.NEWSLETTER_SLACK_CHANNEL, text)
        if not res.get("ok"):
            # Slack unreachable (likely the same outage) — surface it locally so
            # the backstop is never itself silent.
            from .dispatcher import notify_local
            notify_local("Overnight pipeline needs attention — " + issues[0][:120])
    return {"job": "health_alert", "issues": issues or ["healthy"]}


# --- job: headless article-drafter (article-on-pick) ------------------------
def run_newsletter_article_drafter(ctx: dict) -> dict:
    """Headless replacement for the Cowork 'article-drafter': when the user replies
    a pick to the morning digest ("4", "4 to x", "4 to x, 5 to substack"), load
    that angle from today's saved angles, draft the article via the LLM, write it
    as a `newsletter-draft-<day>-angleN.md` with `channels:` frontmatter, and post
    it as "✍️ Newsletter draft ready (angle N)…" — the exact shape the approval-
    poller's publish gate already recognises. Idempotent per angle (ledger-keyed),
    so it drafts each pick once no matter how many ticks see the reply. Only active
    on the api backend; under "inapp" the Cowork task still owns this."""
    if config.DRAFT_BACKEND != "api":
        return {"job": "newsletter_article_drafter", "note": "inapp backend"}
    dry = ctx.get("dry_run", False)
    if not dry and not config.draft_providers():
        # api backend but no key yet → newsletter_article would return a stub;
        # never post that to Slack (same guard as the digest's post_once).
        return {"job": "newsletter_article_drafter", "note": "no provider configured"}
    now = ctx["now"]
    ledger = ctx.get("ledger")
    stamp = now.strftime("%y%m%d")
    angles_file = config.STATE_DIR / f"newsletter-angles-{stamp}.json"
    if not angles_file.exists():
        return {"job": "newsletter_article_drafter", "note": "no angles today"}
    if not dry and not slack_io.has_internet():
        return {"job": "newsletter_article_drafter", "note": "no internet"}
    try:
        by_n = {int(a["n"]): a for a in json.loads(angles_file.read_text(encoding="utf-8"))
                if isinstance(a, dict) and "n" in a}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"job": "newsletter_article_drafter", "note": "angles unreadable"}

    # A pick only counts if it POST-DATES today's digest: a bare "5" left over from
    # a PRIOR day's digest must not draft today's (differently-numbered) angle 5.
    # The angles file's mtime ≈ when today's digest was posted; require the reply
    # to be newer than that (with a small skew), AND recent (replay guard).
    try:
        digest_ts = angles_file.stat().st_mtime
    except OSError:
        digest_ts = 0.0
    cutoff = max(now.timestamp() - 36 * 3600, digest_ts - 60)
    drafted: list[int] = []
    for m in slack_io.read_channel_deep(config.NEWSLETTER_SLACK_CHANNEL, dry_run=dry):
        txt, ts = m.get("text", ""), m.get("ts", "")
        if not txt or not ts or slack_io.is_bot_message(txt):
            continue
        try:
            if float(ts) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        for n, channels in _parse_newsletter_picks(txt):
            if n not in by_n:
                continue
            key = f"nlarticle:{stamp}:angle{n}"
            if ledger and not dry and ledger.seen(key):
                continue
            article = draft.newsletter_article(by_n[n], dry_run=dry)
            body = (f"---\ntitle: {_first_line(article)}\n"
                    f"channels: [{', '.join(channels)}]\n"
                    f"angle: {by_n[n].get('angle', '')}\n---\n{article}")
            if not dry:
                (config.STATE_DIR / f"newsletter-draft-{stamp}-angle{n}.md").write_text(
                    body, encoding="utf-8")
            slack_io.send_message(
                config.NEWSLETTER_SLACK_CHANNEL,
                f"✍️ Newsletter draft ready (angle {n}) — channels: [{', '.join(channels)}]"
                f"\n\n{article}\n\n↩️ Reply `publish` (in-thread) to ship it, or `hold`.",
                dry_run=dry)
            if ledger and not dry:
                ledger.add_seen(key)
            drafted.append(n)
    return {"job": "newsletter_article_drafter",
            "drafted": drafted or ["no new picks"]}


JOBS = [
    Job("daily_scrape_draft", run_daily_scrape_draft, kind="daily_once",
        at=time(1, 0)),                                  # daily 01:00 (also drains)
    Job("approval_poller", run_approval_poller, kind="window_repeat",
        window=(time(6, 0), time(9, 0))),                # 06:00–09:00 (matches publish window)
    Job("newsletter_article_drafter", run_newsletter_article_drafter, kind="window_repeat",
        window=(time(0, 0), time(23, 59))),              # every tick: draft on a pick whenever it lands
    Job("health_alert", run_health_alert, kind="daily_once",
        at=time(6, 0)),                                  # first Window-B wake: recover/notify, never silent
    Job("token_dashboard", run_token_dashboard, kind="weekly_once",
        weekday=6, at=time(1, 5)),                       # Sunday 01:05
    Job("inbox_drain", run_inbox_drain, kind="window_repeat",
        window=(time(0, 0), time(23, 59))),              # every tick, all day (LinkedIn + X)
]
