"""
The only step that needs an LLM's *intelligence* — drafting in-voice content via
a direct chat-completions call (no agent, no Chrome). Provider-agnostic: any
OpenAI-compatible endpoint works, so the model is Gemini, Kimi, or Anthropic
purely by config (config.DRAFT_PROVIDERS + per-provider env overrides), with an
automatic failover chain.

Everything upstream (scrape, rank) is deterministic; this turns ranked signals
into the digest / shortlist / article. If no provider key is present (or
dry_run), returns a clearly-marked stub so the rest of the pipeline stays
testable offline.
"""
from __future__ import annotations

import logging
import textwrap

from . import config

log = logging.getLogger("orchestrator.draft")

# >>> EDIT: your two brand voices. These are the only "intelligence" config —
# keep them in sync with the in-app task prompts in inapp_tasks/.
NEWSLETTER_VOICE = (
    "<<DESCRIBE YOUR NEWSLETTER/PERSONAL VOICE HERE — tone, register, "
    "do's and don'ts, length. e.g. 'dry, warm, one vivid analogy per piece'.>>"
)
COMPANY_VOICE = (
    "Third-person company voice (never 'I'/'we'), direct, no emojis, no buzzwords, "
    "no hedging, no unbacked stats (use 'most'/'many'). "
    "<<ADD ANY COMPANY-SPECIFIC VOICE RULES HERE>>"
)


def _call_llm(system: str, prompt: str, *, max_tokens: int = 1500,
              dry_run: bool = False) -> str:
    """Draft via the provider CHAIN (config.draft_providers()) over an OpenAI-
    compatible API: try each in priority order and fall through on any error, so
    failover is automatic. Returns a clearly-marked stub when no provider is
    configured; raises only if EVERY provider fails — so a stub is never posted as
    if it were real content."""
    providers = [] if dry_run else config.draft_providers()
    if not providers:
        return (f"[draft dry-run | providers={config.DRAFT_PROVIDERS}]\n"
                f"SYSTEM: {system[:80]}...\nPROMPT: {prompt[:160]}...")
    from openai import OpenAI  # lazy import so the package loads without the SDK
    errors = []
    for p in providers:
        try:
            client = OpenAI(api_key=p["api_key"], base_url=p["base_url"])
            resp = client.chat.completions.create(
                model=p["model"], max_tokens=max_tokens,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompt}],
                **p.get("extra", {}),  # e.g. Gemini reasoning_effort="none"
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                if p is not providers[0]:
                    log.warning("draft: fell back to %s/%s", p["name"], p["model"])
                return text
            errors.append(f"{p['name']}: empty response")
        except Exception as exc:  # any provider error → try the next
            errors.append(f"{p['name']}: {exc}")
            log.warning("draft: provider %s failed (%s) — trying next", p["name"], exc)
    raise RuntimeError("all draft providers failed: " + " | ".join(errors))


def _signal_lines(signals: list[dict], limit: int) -> str:
    rows = []
    for i, s in enumerate(signals[:limit], 1):
        rows.append(f"{i}. [{s['source']}] {s.get('text','')[:200]}  "
                    f"(velocity={s.get('velocity',0):.1f}; {s['url']})")
    return "\n".join(rows) if rows else "(no signals)"


def newsletter_digest(signals: list[dict], *, dry_run: bool = False) -> str:
    prompt = textwrap.dedent(f"""
        From these ranked signals, frame 3–5 article
        angles. Each = one-line hook + the angle you'd take + why it's timely
        + the source link. Open with: reply a number to draft, 'skip' to pass.

        {_signal_lines(signals, 12)}
    """).strip()
    return _call_llm(NEWSLETTER_VOICE, prompt, dry_run=dry_run)


def newsletter_angles(signals: list[dict], *, dry_run: bool = False) -> list[dict]:
    """Structured digest: 3–5 article angles as [{n, hook, angle, source}] so the
    article-drafter can map a pick number back to its angle. One LLM call; the
    daily job formats these into the Slack digest AND saves the JSON for picks.
    Returns [] if the model didn't return parseable JSON (job falls back to the
    free-text digest)."""
    prompt = textwrap.dedent(f"""
        From these ranked signals, propose 3–5 article angles. Return ONLY a JSON
        array — no prose, no code fence. Each item:
        {{"n": <1-based int>, "hook": "<punchy hook, ≤14 words>", "angle": "<the angle to take, one sentence>", "source": "<the EXACT url of the signal this draws from, copied from the list below>"}}

        {_signal_lines(signals, 12)}
    """).strip()
    return _parse_json_array(_call_llm(NEWSLETTER_VOICE, prompt, max_tokens=1400, dry_run=dry_run))


def _parse_json_array(raw: str) -> list[dict]:
    import json
    import re
    if not raw or raw.lstrip().startswith("[draft dry-run"):
        return []
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()).strip()
    m = re.search(r"\[.*\]", s, re.S)
    if m:
        s = m.group(0)
    try:
        data = json.loads(s)
    except Exception:
        return []
    return [a for a in data if isinstance(a, dict) and "n" in a] if isinstance(data, list) else []


def newsletter_article(idea: dict, *, dry_run: bool = False) -> str:
    prompt = textwrap.dedent(f"""
        Write your article on this angle (~800–1100 words), title + one-line
        subtitle. Verify every fact; invent no statistics.

        ANGLE: {idea.get('angle') or idea.get('text', '')}
        SOURCE: {idea.get('source') or idea.get('url', '')}
    """).strip()
    return _call_llm(NEWSLETTER_VOICE, prompt, max_tokens=3000, dry_run=dry_run)


def company_shortlist(signals: list[dict], *, dry_run: bool = False) -> str:
    prompt = textwrap.dedent(f"""
        These are today's accelerating signals. For the
        top 3 LinkedIn-replyable items, draft a value-first company-voice reply
        (2–4 sentences, no link drop). Number them R1, R2, R3 and include each
        post link. Then a line: reply with numbers to approve (e.g. `R1 R3`), or `skip`.

        {_signal_lines(signals, 8)}
    """).strip()
    return _call_llm(COMPANY_VOICE, prompt, dry_run=dry_run)


def company_weekly_post(theme_signals: list[dict], *, dry_run: bool = False) -> str:
    prompt = textwrap.dedent(f"""
        Pick the single strongest accelerating theme below and draft ONE original
        company-voice LinkedIn post tying it to your company's positioning.
        Propose a landing URL and a UTM tag.

        {_signal_lines(theme_signals, 10)}
    """).strip()
    return _call_llm(COMPANY_VOICE, prompt, dry_run=dry_run)
