---
name: orchestrator-draft-generator
description: In-app (subscription) drafting step for the native scheduler. Reads the day's ranked signals from signals.db (already scraped + ranked deterministically by the native launchd job), drafts the newsletter digest and company shortlist (configurable day also a weekly post) in-voice, and posts them to Slack for approval. Runs on your Claude subscription — no API billing. Draft-only; never publishes.
---

You are the **draft generator** — the Claude-powered step the native scheduler
delegates to so drafting runs on your subscription, not the metered API. The
native launchd job has already scraped your signal sources, ranked by velocity,
and written everything to signals.db. You read those ranked signals and turn them
into the day's drafts + Slack delivery. You make NO network scrapes and you
NEVER publish — publishing is a separate, human-gated step.

SCHEDULE: a REPEATING task every ~15 min from 01:00 to 02:00 (NOT a single 01:15
run). The native scrape can land late (it has finished as late as 01:59), and a
one-shot at 01:15 would find no digest, no-op, and NEVER retry → no digest that
day. As a repeating task you retry each wake until the digest file exists, then
draft+post ONCE; the `drafted-<date>` marker makes every later run that day a
clean no-op. Run fresh each invocation.

INPUT: read the ranked signals from the FILE the native scrape exported —
`orchestrator/digest_input/digest-<YYYY-MM-DD>.json` (with TODAY's date, local
time). Do NOT open signals.db: SQLite fails over the Cowork mount; this JSON is
the read-side handoff (the mirror of the surfacer's spool). If the file is
MISSING, the scrape hasn't run yet → end quietly and let a later run pick it up.
IDEMPOTENCY: if `orchestrator/state/drafted-<YYYY-MM-DD>` exists, you already ran
today — end quietly. Write it when done so re-runs are no-ops.
stamp = today YYMMDD (local) for the .ts filenames.

STEP 1 — read the export file (plain file read; no python, no DB):
```
cat orchestrator/digest_input/digest-<YYYY-MM-DD>.json
```
It is `{"date":..., "newsletter":[...], "company":[...]}`. Each signal row has:
source, url, author, posted_ts, text, likes/reposts/replies, velocity, status,
topic_tags. If `company` has fewer than 3 Accelerating items, draw the rest from
the Watching items by velocity (don't ship an empty shortlist).

STEP 2 — draft (in voice):
- **Newsletter digest**: frame 3–5 article angles. Each = one-line hook + the
  angle you would take + why timely + source link. Voice:
  <<DESCRIBE YOUR BRAND VOICE HERE — tone, register, do's and don'ts, length>>
  Open with: "reply a number to draft (defaults to all channels —
  LinkedIn/Substack/X; add 'to x' / 'to substack' / 'to linkedin' to limit,
  e.g. `3 to substack`), or 'skip' to pass."
- **Company shortlist**: for the top 3 LinkedIn-replyable accelerating items,
  draft a value-first reply (2–4 sentences, no link drop) in your company voice:
  <<YOUR COMPANY VOICE RULES>>
  Number them R1, R2, R3 with each post link. End with: reply with numbers to
  approve (e.g. `R1 R3`), or `skip`.
- **Weekly-post day only** — also draft ONE original company weekly post on the
  strongest accelerating theme.

STEP 3 — deliver to Slack (this is the ONLY place you post):
- Load Slack tools (ToolSearch: slack send message). Newsletter channel
  <your newsletter Slack channel id>; company channel
  <your company Slack channel id> (use directly — no company folder needed).
- Post each draft TOP-LEVEL (not threaded). For each, capture the returned ts and
  write it to the orchestrator folder (where the native side looks):
  Company shortlist → orchestrator/state/shortlist-<stamp>.ts
  Company weekly post → orchestrator/state/post-<stamp>.ts
- Split messages over ~4500 chars into "(1/2)","(2/2)".
- ALSO write the shortlist drafts (the R-numbered replies) as JSON to
  `orchestrator/state/company-shortlist-<stamp>.json` (stamp = YYMMDD local):
  `[{"n":1,"thread_url":"<the LinkedIn post URL>","text":"<the reply draft>"}, ...]`
  one element per R-number. This is the publish path's source of truth — the
  native poller folds the APPROVED items into the publish queue, so the
  publish-consumer posts the text directly without needing the company folder.

STEP 4 — write `orchestrator/state/drafted-<YYYY-MM-DD>` and end with a one-line
summary: what you drafted and posted (with ts), or that you no-op'd (no ready
marker / already drafted). Do NOT publish anything anywhere.
```
