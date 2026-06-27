"""
The only step that needs Claude's *intelligence* — drafting in-voice content
via a direct Anthropic API call (no agent, no Chrome).

Everything upstream (scrape, rank) is deterministic; this turns ranked signals
into the digest / shortlist / article / comments. If no API key is present (or
dry_run), returns a clearly-marked stub so the rest of the pipeline stays
testable offline.
"""
from __future__ import annotations

import textwrap

from . import config

MODEL = "claude-opus-4-8"

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


def _call_claude(system: str, prompt: str, *, max_tokens: int = 1500,
                 dry_run: bool = False) -> str:
    if dry_run or not config.ANTHROPIC_API_KEY:
        return f"[draft dry-run | model={MODEL}]\nSYSTEM: {system[:80]}...\nPROMPT: {prompt[:160]}..."
    # Imported lazily so the package loads without the SDK installed.
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


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
    return _call_claude(NEWSLETTER_VOICE, prompt, dry_run=dry_run)


def newsletter_article(idea: dict, *, dry_run: bool = False) -> str:
    prompt = textwrap.dedent(f"""
        Write your article on this angle (~800–1100 words), title + one-line
        subtitle. Verify every fact; invent no statistics.

        ANGLE: {idea.get('text','')}
        SOURCE: {idea.get('url','')}
    """).strip()
    return _call_claude(NEWSLETTER_VOICE, prompt, max_tokens=3000, dry_run=dry_run)


def company_shortlist(signals: list[dict], *, dry_run: bool = False) -> str:
    prompt = textwrap.dedent(f"""
        These are today's accelerating signals. For the
        top 3 LinkedIn-replyable items, draft a value-first company-voice reply
        (2–4 sentences, no link drop). Number them R1, R2, R3 and include each
        post link. Then a line: reply with numbers to approve (e.g. `R1 R3`), or `skip`.

        {_signal_lines(signals, 8)}
    """).strip()
    return _call_claude(COMPANY_VOICE, prompt, dry_run=dry_run)


def company_weekly_post(theme_signals: list[dict], *, dry_run: bool = False) -> str:
    prompt = textwrap.dedent(f"""
        Pick the single strongest accelerating theme below and draft ONE original
        company-voice LinkedIn post tying it to your company's positioning.
        Propose a landing URL and a UTM tag.

        {_signal_lines(theme_signals, 10)}
    """).strip()
    return _call_claude(COMPANY_VOICE, prompt, dry_run=dry_run)
