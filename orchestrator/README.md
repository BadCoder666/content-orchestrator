# `orchestrator/` — package dev notes

This package is the unified native scheduler: one launchd job, a ~12-minute
heartbeat (`dispatcher.py`) that reads an internal cron manifest and runs
whatever is due. It coordinates two example projects — a **company** stream and a
**newsletter** stream — and folds in a weekly **token-usage dashboard**.

> Setup, install, and environment instructions live in the **repo-root
> `README.md`**. This file is just how the modules fit together and how to run
> the checks.

## The deterministic / Claude split

The design keeps a hard line between deterministic Python (on launchd) and
Claude-powered work (in-app Cowork tasks on a subscription, no metered API):

- **No Claude (native launchd, pure Python):** scheduling, scraping (Reddit JSON,
  Substack RSS), velocity/acceleration ranking, registers, Slack send/poll/parse,
  the token dashboard, and the `chrome_queue` / `signals.db` / marker seams.
- **Claude on the subscription (in-app Cowork tasks):** all in-voice drafting
  (`draft-generator`), LinkedIn reading (`linkedin-surfacer`), and publishing
  (`publish-consumer`). Chosen over the direct API so there's no metered billing.
  Tradeoff: the Claude app must be running for these to fire. `claude -p` headless
  401s (subscription OAuth isn't available to background processes), so these stay
  in-app, not on launchd.
- **API fallback:** set `config.DRAFT_BACKEND = "api"` (+ `ANTHROPIC_API_KEY`) to
  move drafting back inline into the native job via `draft.py` — reversible if the
  in-app task proves unreliable.

## Modules

```
dispatcher.py     heartbeat entrypoint (single-instance locked)
schedule.py       Job cron-logic + atomic idempotency Ledger
jobs.py           the jobs + the manifest (JOBS)
scraper.py        combined Chrome-free scraper → signals.db
store.py          signals.db schema + upsert/dedupe/query
rank.py           velocity / acceleration / project segregation
draft.py          Anthropic API drafting (dry-run safe)
slack_io.py       Slack send/read + deterministic reply parser
chrome_queue.py   file-handoff seam to the in-app Chrome tasks
store_signal.py   CLI the in-app surfacers (LinkedIn, Reddit) use to write into signals.db
verify.py         offline check suite + `--live` smoke test
inapp_tasks/      the thin Cowork/Chrome consumer task prompts
SURFACER_NOTES.md how-to for the in-app LinkedIn surfacer
```

## In-app (subscription) tasks

These run on the subscription via the Claude app (not launchd) and read/write the
shared store the native jobs fill:

- `inapp_tasks/draft-generator.SKILL.md` — reads ranked `signals.db` → drafts the
  digest/shortlist/post in-voice → posts to Slack. Idempotent per-day.
- `inapp_tasks/linkedin-surfacer.SKILL.md` — reads LinkedIn (no headless path) →
  writes signals into `signals.db`. See `SURFACER_NOTES.md`.
- `inapp_tasks/publish-consumer.SKILL.md` — consumes `chrome_queue/` approvals →
  publishes via Chrome. Idempotent per-request.

`draft-generator` and `publish-consumer` are idempotent (per-day / per-request
markers), so extra fires are safe no-ops.

## Running the checks

From the repo root (with the project venv active):

```bash
venv/bin/python -m orchestrator.verify          # offline check suite
venv/bin/python -m orchestrator.verify --live   # network + Slack smoke test
venv/bin/python -m orchestrator.dispatcher --list   # show what's due / ticks
```

The offline suite is self-contained. Live network / Anthropic / Chrome paths are
only exercised by `verify --live` and the in-app tasks — the offline suite cannot
reach them.

## Known limits

- **Reddit:** public JSON is 403-blocked on many IPs. Set `REDDIT_CLIENT_ID` +
  `REDDIT_CLIENT_SECRET` (free "script" app, application-only OAuth) in the env
  file; the scraper uses `oauth.reddit.com` when present, public JSON otherwise.
- **X:** no reliable no-key bridge confirmed → `scraper` skips X cleanly until
  `config.X_HANDLES` is populated with a validated endpoint.
- **LinkedIn:** read only via the in-app Chrome surfacer (no headless path).
