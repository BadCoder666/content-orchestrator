"""
Central configuration for the unified Claude scheduler/orchestrator.

One place to declare: filesystem paths, the per-project theses (deterministic
keyword filters used to segregate the shared scrape), and the source registries.

No secrets here — tokens come from the environment (export them in a private
env-file your launchd wrapper sources; see .env.example). Deployment-specific
values (channels, paths, Wi-Fi, feeds) are read from the environment too, with
neutral example defaults, so you can configure without editing code.

This template ships with TWO example projects — `company` (a brand/marketing
feed that posts LinkedIn comments) and `newsletter` (a personal publication feed
that posts articles to multiple channels). Rename/replace them for your use.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# --- Time -------------------------------------------------------------------
# launchd fires on the machine's local clock; we bucket on the same local zone.
LOCAL_TZ = ZoneInfo(_env("ORCH_TZ", "America/New_York"))

# --- Paths ------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

STATE_DIR = HERE / "state"            # run-ledger markers (idempotency)
QUEUE_DIR = HERE / "chrome_queue"     # file-handoff seam to in-app Chrome tasks
SIGNALS_DB = HERE / "signals.db"      # shared scrape store
DIGEST_INPUT_DIR = HERE / "digest_input"  # native exports ranked signals here as
#   plain JSON so the in-app (Cowork) drafter can READ them — it can't open
#   signals.db over its FUSE mount, but file reads work fine.
LOG_DIR = Path(_env("ORCH_LOG_DIR", str(Path.home() / "Documents" / "claude-orchestrator" / "logs")))

# External project locations (optional — read/write their existing registers in
# place). Point these at your own project folders, or leave the defaults if you
# don't use the external-folder integrations.
COMPANY_REPO = Path(_env("COMPANY_REPO", str(Path.home() / "projects" / "company")))
COMPANY_MARKETING = COMPANY_REPO / "marketing"
NEWSLETTER_DIR = Path(_env("NEWSLETTER_DIR", str(Path.home() / "projects" / "newsletter")))

# Optional external weekly script folded into the scheduler as a `weekly_once`
# job (see jobs.run_token_dashboard for the pattern). Supply your own or remove
# the job from JOBS if unused.
DASHBOARD_PY = Path(_env("DASHBOARD_PY", str(PROJECT_ROOT / "dashboard.py")))
DASHBOARD_CONFIG = Path(_env("DASHBOARD_CONFIG", str(PROJECT_ROOT / "config.json")))
DASHBOARD_PYTHON = Path(_env("DASHBOARD_PYTHON", str(PROJECT_ROOT / "venv" / "bin" / "python")))

# --- Secrets (from env, NEVER hard-coded) -----------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Free Reddit "script" app (reddit.com/prefs/apps) — application-only OAuth.
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")

# --- Network (overnight resilience) -----------------------------------------
# The scrape + Slack delivery need internet at ~01:00 / 06:00 when the laptop may
# have dropped Wi-Fi. ensure_network.sh brings Wi-Fi up and, if the primary
# network is unreachable, falls back to a phone hotspot. macOS auto-joins the top
# preferred network in range; HOTSPOT_SSIDS are the explicit fallbacks it tries
# in order if auto-join doesn't restore internet. Set these to your phone
# hotspot's SSID(s); the hotspot must be broadcasting.
WIFI_IFACE = _env("WIFI_IFACE", "en0")
HOTSPOT_SSIDS = [s for s in _env("HOTSPOT_SSIDS", "My Phone").split(",") if s]

# --- Slack channels (set to your channel IDs) -------------------------------
NEWSLETTER_SLACK_CHANNEL = _env("NEWSLETTER_SLACK_CHANNEL", "C00000NEWS")
# Company channel id is read from a file in the project repo at runtime; fallback here.
COMPANY_SLACK_FALLBACK = _env("COMPANY_SLACK_FALLBACK", "C00000COMP")


def company_channel() -> str:
    f = COMPANY_MARKETING / "slack" / "channel.txt"
    try:
        return f.read_text(encoding="utf-8").strip() or COMPANY_SLACK_FALLBACK
    except OSError:
        return COMPANY_SLACK_FALLBACK


# --- Theses (deterministic segregation filters) ------------------------------
# EXAMPLE keywords — replace with your own topics. Lowercase substrings; a scraped
# signal is tagged for a project if its text matches that project's keywords. This
# is the cheap, debuggable filter that segregates the shared scrape; draft.py
# (Claude) does the nuanced work later.
COMPANY_KEYWORDS = [
    "ecommerce", "e-commerce", "shopify", "product data", "catalog",
    "digital shelf", "pim", "pxm", "structured data", "schema.org", "json-ld",
    "feed quality", "agentic commerce", "ai shopping", "ai overviews",
    "perplexity", "chatgpt shopping", "rufus", "geo", "aeo", "merchandising",
]
NEWSLETTER_KEYWORDS = [
    "space", "nasa", "spacex", "rocket", "orbit", "satellite", "lunar",
    "moon", "mars", "astronaut", "space economy", "launch", "starship",
]

PROJECTS = ("company", "newsletter")

# Drafting backend. "inapp" (default): the native job only scrapes+ranks and
# drops a ready-marker; a Claude-app Cowork task (inapp_tasks/draft-generator)
# does the in-voice drafting + Slack delivery on a subscription (no API billing).
# "api": the native job drafts via draft.py/Anthropic API and posts itself —
# fully headless/unattended (small per-run API cost; needs ANTHROPIC_API_KEY).
DRAFT_BACKEND = _env("DRAFT_BACKEND", "inapp")

# --- Source registries (EXAMPLE values — replace) ----------------------------
# Reddit subs to watch (public search.json — no Chrome).
REDDIT_SUBS = ["shopify", "ecommerce", "bigseo", "SEO"]
REDDIT_KEYWORDS = [
    "ChatGPT", "AI shopping", "AI agent", "AI Overviews", "Perplexity",
    "schema.org", "JSON-LD", "structured data", "GEO", "agentic",
]

# Substack publications to pull RSS from (no Chrome). Add your own feed URLs.
SUBSTACK_FEEDS = [s for s in _env("SUBSTACK_FEEDS", "https://example.substack.com/feed").split(",") if s]

# X handles. Left empty by design: as of 2026 there is NO reliable no-key X
# source — nitter is dead, bridges carry no engagement counts (so they can't feed
# velocity ranking) or 403/timeout. Enable only with a real X API key (set
# X_BEARER_TOKEN and wire scraper.fetch_x accordingly), or use the in-app X
# surfacer (inapp_tasks/x-surfacer) which reads via the browser.
X_HANDLES: list[str] = []

# Reddit requires a unique descriptive UA in their recommended format. Put your
# own Reddit username here.
USER_AGENT = _env("REDDIT_USER_AGENT", "macos:claude-orchestrator:v0.1 (by /u/your_username)")
