# claude-orchestrator

A **deterministic, subscription-friendly content orchestrator** for macOS. It scrapes
signals from Reddit, Substack, X, and LinkedIn on a schedule, ranks them by
engagement *velocity*, segregates them per project, and turns the strongest into
drafts that wait for a one-word **human approval in Slack** before anything is
published.

The core runs as a native `launchd` job with **no LLM at runtime** — it's fully
deterministic and testable. The writing steps run on **Claude** (either your
Claude.ai subscription via in-app Cowork tasks, or the Anthropic API), and
publishing is gated behind an explicit human approval. Nothing posts to a public
platform without you replying `publish`.

> Ships with two example projects — `company` (a brand feed that posts LinkedIn
> comments) and `newsletter` (a personal publication that posts articles to X /
> Substack / LinkedIn). Rename/replace them for your own use.

## Why this exists

Most "AI agent reads social media" tools are cookie-scrapers that risk banning the
very accounts you publish from, return text without engagement counts, and break
unattended when a session cookie expires. This takes the opposite stance:

- **Official/sanctioned reads where they exist** — Reddit OAuth, Substack RSS. No
  cookies, no ban risk, fully headless.
- **Human-in-the-loop reads where no API exists** — X and LinkedIn are read via
  in-app Cowork tasks driving your own browser, on your terms.
- **Deterministic core** — ranking, dedup, segregation, scheduling, and
  idempotency are plain Python with a 115-check offline test suite. The LLM only
  writes prose; it never decides what's true or what publishes.
- **Engagement-aware** — ranks by `likes + 2·reposts + 3·replies` over time, so
  you surface what's *accelerating*, not just what's recent.

## Architecture

```
            ┌─────────────── native launchd dispatcher (every ~12 min) ───────────────┐
            │  scrape → rank (velocity) → segregate per project → export digest JSON   │
            └─────────────────────────────────────────────────────────────────────────┘
                                          │  (file handoff: digest_input/*.json)
                                          ▼
            ┌──────────── Claude drafting (subscription Cowork task, or API) ──────────┐
            │  reads the ranked signals → drafts digest + shortlist → posts to Slack   │
            └─────────────────────────────────────────────────────────────────────────┘
                                          │  you reply  publish / R1 R3 / 4 to x
                                          ▼
            ┌──────────── native approval poller (06:00–09:00) ───────────────────────┐
            │  reads Slack, parses approvals, enqueues approved publishes             │
            └─────────────────────────────────────────────────────────────────────────┘
                                          │  (file handoff: chrome_queue/*.json)
                                          ▼
            ┌──────────── publish consumer (in-app, browser) ─────────────────────────┐
            │  posts the approved item to X / Substack / LinkedIn; confirms in Slack   │
            └─────────────────────────────────────────────────────────────────────────┘
```

The **file-handoff seam** (`digest_input/`, `chrome_queue/`) is deliberate: the
in-app Cowork tasks see the project folder as a FUSE mount where SQLite writes
fail, so the native side exports/consumes plain JSON files instead of sharing the
DB. This lets the deterministic launchd half and the Claude/browser half cooperate
without a shared database.

## Sources

| Source | How it's read | Headless? | Engagement counts? |
|---|---|---|---|
| **Reddit** | official application-only OAuth (`search.json`) | ✅ yes | ✅ |
| **Substack** | public RSS (`feedparser`) | ✅ yes | n/a (post text) |
| **X** | in-app surfacer (browser) → JSON spool, or X API if you have a key | in-app | ✅ |
| **LinkedIn** | in-app surfacer (browser) → JSON spool | in-app | ✅ |

X/LinkedIn have no sanctioned read API, so they're surfaced by Cowork tasks
(`orchestrator/inapp_tasks/`) that drop JSON into a spool the native job drains.

## Repo layout

```
orchestrator/
  dispatcher.py      # the single launchd entrypoint; ticks every ~12 min
  jobs.py            # the job manifest (scrape+draft, approval poller, health alert, …)
  schedule.py        # cron-style due-logic + idempotency ledger
  scraper.py         # Reddit OAuth + Substack RSS (no Chrome)
  rank.py            # velocity/acceleration ranking + project segregation + URL canonicalization
  store.py           # SQLite signal store (upsert/dedup, day-aware prev-velocity)
  slack_io.py        # Slack send + read + the deterministic approval parser
  ingest_inbox.py    # drains the X/LinkedIn surfacer spools
  digests.py draft.py store_signal.py chrome_queue.py
  config.py          # all config (env-driven; secrets from env only)
  verify.py          # 115-check offline test suite (the "self-test")
  wake/              # overnight reliability: pmset wake-armer + Wi-Fi ensure script
  inapp_tasks/       # the Cowork task prompts (drafting, surfacing, publishing)
.env.example  requirements.txt  LICENSE
```

## Setup

1. **Install deps** (Python 3.10+):
   ```bash
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Configure**: `cp .env.example .env`, fill it in. See `config.py` for everything
   that's env-driven (timezone, channels, feeds, hotspot SSIDs, draft backend).
3. **Slack bot**: create an app, add scopes `chat:write, channels:history,
   channels:read, groups:history, groups:read`, install it, and **invite the bot to
   each channel** it must read/post in.
4. **Run the tests** to confirm the engine is healthy:
   ```bash
   python -m orchestrator.verify          # 115 offline checks
   python -m orchestrator.dispatcher --selftest   # overnight-reliability machinery
   ```
5. **Install the launchd agent** (`orchestrator/com.example.claude-orchestrator.plist`)
   — edit the `/Users/YOURNAME/...` paths, then `launchctl load` it.
6. **Overnight wakes** (macOS laptop, lid closed): see `orchestrator/wake/` — a root
   LaunchDaemon arms RTC wakes and holds the Mac awake with `pmset -a disablesleep`
   during the batch window, with a Wi-Fi→phone-hotspot fallback.
7. **Wire the Claude side**: the prompts in `orchestrator/inapp_tasks/` are the
   Cowork scheduled tasks that do the drafting, surfacing, and publishing on your
   subscription. (Or set `DRAFT_BACKEND=api` for fully-headless drafting via the
   Anthropic API.)

## Drafting backend: `inapp` vs `api`

- **`inapp`** (default) — drafting runs as a Claude.ai **Cowork task** on your
  subscription (no API billing). Trade-off: it needs the Claude desktop app open;
  it can't run fully unattended with the lid shut. A morning `health_alert` job
  detects a missed draft and pings you to run it.
- **`api`** — the native job drafts via the Anthropic API and posts itself. Fully
  headless and unattended (small per-run cost; needs `ANTHROPIC_API_KEY`).

## Reliability

- **Idempotent** — a once-per-day job that runs twice (heartbeat + manual) is a
  no-op the second time, via the slot ledger.
- **Overnight on a laptop** — RTC wakes fire on battery; `pmset -a disablesleep`
  beats clamshell sleep; `ensure_network.sh` brings Wi-Fi up and falls back to a
  phone hotspot; a fail-loud `health_alert` Slack-pings (and falls back to a local
  macOS notification) if the pipeline didn't deliver — so it never fails silently.
- **Tested** — `python -m orchestrator.verify` exercises every deterministic
  component offline (no network, no Claude, no Slack, no Chrome).

## Status

Working software extracted from a personal deployment, generalized into a template.
The two example projects, keywords, and prompts are illustrative — swap in your own.
PRs welcome.

## License

MIT — see `LICENSE`.
