"""
Friday weekly digests — deterministic, native (no Claude).

Reads the Company marketing registers, appends a structured weekly rollup to each
`weekly_log.md`, and posts a one-line Slack summary. Idempotent: if this week's
section already exists in the log it's a no-op, so retries/force-runs never
duplicate. Qualitative lines ("emerging trend") are left as TBD placeholders —
that's the only LLM-ish bit and it stays out of the native path.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, slack_io


def week_bounds(now) -> tuple[date, date]:
    """Monday→Friday of the week containing `now`."""
    d = now.date()
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=4)


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _rows_in_week(csv_path: Path, mon: date, fri: date) -> list[dict]:
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = _parse_date(row.get("date", ""))
                if d and mon <= d <= fri:
                    rows.append(row)
    except OSError:
        return []
    return rows


def _has_section(md_path: Path, header: str) -> bool:
    try:
        return header in md_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _append(md_path: Path, text: str) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with md_path.open("a", encoding="utf-8") as f:
        f.write(text)


def _linkedin(mon: date, fri: date) -> tuple[str, str, dict]:
    rows = _rows_in_week(config.COMPANY_MARKETING / "linkedin" / "comment_log.csv", mon, fri)
    posted = sum(1 for r in rows if r.get("status", "").strip().lower() == "posted")
    suggested = sum(1 for r in rows if r.get("status", "").strip().lower() == "suggested")
    header = f"## Week ending {fri.isoformat()}"
    section = (f"\n{header}\n\n"
               f"- Comments seeded: {len(rows)} (posted: {posted}, backlog: {suggested})\n"
               f"- Emerging trend worth a future post: _TBD — add observation_\n"
               f"- Pipeline status: _TBD_\n")
    return header, section, {"week_rows": len(rows), "posted": posted}


def _reddit(mon: date, fri: date) -> tuple[str, str, dict]:
    rows = _rows_in_week(config.COMPANY_MARKETING / "reddit" / "engagement_log.csv", mon, fri)
    header = f"## Week of {mon.isoformat()} → {fri.isoformat()}"
    section = (f"\n{header}\n\n"
               f"- Threads engaged: {len(rows)}\n"
               f"- Upvotes / replies / DMs: _TBD — pull metrics_\n"
               f"- Mod actions: none noted\n")
    return header, section, {"week_rows": len(rows)}


def run_friday_digests(now, *, dry: bool = False) -> dict:
    mon, fri = week_bounds(now)
    result: dict = {}
    any_written = False
    md_paths: dict = {}

    for kind, build, md_rel in (
        ("linkedin", _linkedin, "linkedin/weekly_log.md"),
        ("reddit", _reddit, "reddit/weekly_log.md"),
    ):
        header, section, stats = build(mon, fri)
        md = config.COMPANY_MARKETING / md_rel
        md_paths[kind] = (md, header)
        if _has_section(md, header):
            result[kind] = {"already-written": True, **stats}
            continue
        if not dry:
            _append(md, section)
            any_written = True
        result[kind] = {"written": not dry, **stats}

    # Post the summary once, only when this run wrote something AND both sections
    # are now present. This avoids a duplicate ping in the rare crash-recovery
    # case (linkedin written one day, reddit completed the next).
    both_present = all(_has_section(md, hdr) for md, hdr in md_paths.values())
    if any_written and both_present and not dry:
        slack_io.send_message(
            config.company_channel(),
            f"📊 Friday digest — week ending {fri.isoformat()}\n"
            f"LinkedIn: {result['linkedin'].get('week_rows', 0)} comments "
            f"({result['linkedin'].get('posted', 0)} posted)  ·  "
            f"Reddit: {result['reddit'].get('week_rows', 0)} threads",
            dry_run=dry)
    return result
