---
name: x-surfacer-daily
description: In-app X (Twitter) surfacer (runs daily) — reads X from your logged-in session via Claude in Chrome, captures on-thesis posts with engagement, and drops a JSON file in orchestrator/x_inbox/ for the native heartbeat to ingest. Read-only; never posts.
---

cd "/Users/YOURNAME/claude-orchestrator" first. You are the X (Twitter) surfacer. READ-ONLY on X — never post, reply, like, repost, or follow.

IMPORTANT: do NOT write signals.db or run store_signal (SQLite fails over your sandbox mount). Instead, DROP a JSON file into orchestrator/x_inbox/ ; the native heartbeat (`inbox_drain`) ingests it within ~12 min, tagged source=x.

Browser: use Claude in Chrome on your logged-in X session (account <your-handle>). If several browsers are connected and the run is unattended, pick the one already logged into x.com; if none is logged in or no browser is connected, write nothing and stop with a one-line note. Open your OWN new tab; never reuse or close the user's tabs.

WHAT TO READ — scan the last ~7 days for posts on BOTH theses (so the drain can tag for either project):
- Company thesis: <<your target keywords/accounts>>.
- Newsletter thesis: <<your target keywords/accounts>>.
Per topic use the TOP (relevance) tab, which returns posts that have ACCRUED engagement (date/"Latest" returns 0-engagement posts that rank as "Watching" and get dropped): `https://x.com/search?q=<TOPIC>&src=typed_query&f=top`

CAPTURE per post (X exposes these more cleanly than LinkedIn):
- url: read the FULL canonical permalink from the timestamp anchor's `.href`,
  VERBATIM — do not re-type, shorten, or re-derive the id. In JS, from the post
  container `node`: `const a = node.querySelector('a[href*="/status/"]'); a && a.href`
  → `https://x.com/<handle>/status/<id>` with the full **~19-digit** id. Strip any
  `?query`. Sanity check: a real tweet id is ~19 digits — if yours is ~12, you
  read the wrong number; go back to the anchor's `.href`.
- posted: the `<time datetime="…">` attribute on that timestamp (already ISO 8601).
- author: the handle / display name on the post.
- text: the tweet text (cap ~300 chars).
- likes / reposts / replies: from the action-bar buttons' aria-labels (e.g. `"123 Likes"`, `"45 reposts"`, `"6 replies"`) or the count spans. Use 0 if not shown; don't fabricate.

GETTING THE DATA OUT (do not improvise): accumulate each record into `localStorage` as you go, then read the payload back in plain ~700-char `slice()` windows and reassemble. Keep each javascript_tool call short; scroll in small separate calls so the tab isn't dropped.

OUTPUT: write ALL captured posts as ONE JSON array file named `orchestrator/x_inbox/x-<YYYYMMDD-HHMMSS>.json` (real timestamp; create the file directly — no python). Each element:
`{"author":"…","url":"https://x.com/<handle>/status/<id>","posted":"<ISO>","text":"…","likes":N,"reposts":N,"replies":N}`
If nothing on-thesis was captured, do not write a file.

Close every tab you opened (never the user's). End with a one-line summary: posts captured + the spool filename.
