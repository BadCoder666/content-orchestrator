# LinkedIn surfacer — how-to (for the daily Cowork run)

The surfacer feeds the native orchestrator. **Read-only on LinkedIn — never
comment, post, like, follow, or connect.** It captures on-thesis posts and drops
them as a JSON file in `orchestrator/linkedin_inbox/`; the dispatcher heartbeat
(`linkedin_inbox_drain` job) stores them into `signals.db` natively. The
surfacer must **not** touch `signals.db` (SQLite writes fail over the Cowork
FUSE mount) and must **not** rank or draft.

## Scope
- Window: posts from roughly the **last 7 days** (`past-week`). NOT 48h — posts
  minutes old have ZERO engagement, which makes velocity 0 → status "Watching" →
  they get dropped at digest-selection time. A 7-day window + relevance sort
  returns posts that have ACCRUED reactions/comments, which the ranking needs.
- Topics: tune to your thesis (e.g. your product/market keywords).
- Voices to favor: the buyer/practitioner segments you care about. (Relevance is
  ultimately decided by the orchestrator's keyword thesis in `config.py`; capture
  broadly and let the drain filter — off-thesis records are dropped
  automatically.)

## Browser
Use Claude in Chrome on your logged-in session. If several browsers are
connected and the run is unattended, pick the one already logged into LinkedIn;
if none is logged in, write nothing and stop. Open your **own** new tab; never
reuse or close your existing tabs.

## Search pattern
Per topic, navigate to the content search, date-sorted, past-week:

```
https://www.linkedin.com/search/results/content/?keywords=<TOPIC>&datePosted=%22past-week%22&sortBy=%22relevance%22
```

Use **`sortBy="relevance"`**, NOT `date_posted`. Relevance returns posts that
already have engagement (so velocity ranks them); date-sort returns minutes-old
posts with 0 reactions that collapse to "Watching" and never reach the digest.
The drain's keyword filter still removes anything off-thesis.

## Capturing each post (the LinkedIn UI hides the obvious handles)
The current LinkedIn web UI uses **obfuscated class names** and exposes **no
`<time datetime>`, no post permalink anchor, and no activity URN** in the DOM.
Work around it with `javascript_tool`:

1. **Post handle + author.** Each post has a control-menu button:
   `main button[aria-label^="Open control menu for post by "]`. The author name
   is the rest of that aria-label. Walk up to the post container = nearest
   ancestor that also contains `button[aria-label="Comment"]`.

2. **Post URL.** There is no anchor to read. Instead hook the clipboard once:
   ```js
   window.__cap=[]; const o=navigator.clipboard.writeText.bind(navigator.clipboard);
   navigator.clipboard.writeText=t=>{window.__cap.push(t);return o(t).catch(()=>{});};
   ```
   Then per post: click the control-menu button, click the menu item
   **"Copy link to post"**, and **poll** `window.__cap` for the new URL (LinkedIn
   calls `writeText` ~1–1.5s LATER, so clear `window.__cap=[]` immediately before
   the copy-click and take the next entry — otherwise you bind the previous
   post's URL). Strip the `?` query string.

3. **Timestamp.** Derive it from the post id embedded in the URL — the first 41
   bits are the ms epoch:
   ```js
   const id=url.match(/-(\d{15,})-/)[1];
   new Date(Number(BigInt(id)>>22n)).toISOString();
   ```
   This is exact — far better than the relative "5h"/"2d" label.

4. **Counts (no fabrication — 0 if not shown).** They are the `innerText` of the
   action buttons inside the container:
   - reactions = `button[aria-label^="Reaction button state"]`
   - comments  = `button[aria-label="Comment"]`
   - reposts   = `button[aria-label="Repost"]`
   Empty text = 0. Parse `1.2K` / `3M` suffixes.

5. **Text.** Container `innerText`, minus the header (everything up to and
   including the relative-time line) and trailing chrome (`Follow`, `Subscribe`,
   `… more`, the repeated author name, and bare trailing count numbers).

Keep each JS call short (a few seconds). Long-running scrolling loops inside one
`javascript_tool` call can drop the tab — scroll in small separate calls.

## Getting the captured JSON out of the page
The `javascript_tool` result display caps at ~800 chars, so a full run's JSON
(20k+ chars) won't come back in one return. Use this method:

1. As you capture each post, push the cleaned record into `localStorage`
   (persists across same-origin scrolls/navigation):
   `let a=JSON.parse(localStorage.__cap||"[]"); a.push(rec); localStorage.__cap=JSON.stringify(a);`
2. **Cap each record's `text` to ~300 chars** before storing — shrinks the payload.
3. At the end, read it back in fixed **plain ~700-char slices** and reassemble:
   `localStorage.__cap.length` then per slice `localStorage.__cap.slice(i, i+700)`.
   Increment `i` by 700 until you've read the whole length; concatenate verbatim.

Read the payload out in small plain `slice()` windows of plain JSON. Do not use
the clipboard for the bulk JSON (clipboard write is blocked here).

## Output
Write ONE file per run: `orchestrator/linkedin_inbox/<YYYYMMDD-HHMMSS>.json`,
a JSON **array** of records:

```json
[{"author":"…","url":"https://www.linkedin.com/posts/…","posted":"2026-01-01T04:29:47Z",
  "text":"…","likes":0,"reposts":0,"replies":0}]
```

Field notes: `posted` = ISO 8601; `likes` = reactions, `replies` = comments,
`reposts` = reposts; use `0` when a count isn't visible. Filtering to ~48h is
best-effort by the post timestamp. Dedup is by `url` downstream, so a post seen
on two days won't double-store.

Close every tab you opened, then end with a one-line summary. The drain does the
rest within ~12 minutes.
