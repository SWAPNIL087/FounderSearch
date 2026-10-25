# FounderSearch: Design Document

| | |
|---|---|
| **Status** | Phase 2 complete (ingest, storage, classification, API, dashboard) |
| **Audience** | Single user, local machine |
| **Stack** | Python 3.10+, FastAPI, SQLite, vanilla JS |

## 1. Purpose
Find **problems** (complaints, unmet needs, especially about products people already pay for) and **clients** (people
asking for a developer or a tool) across public sources, without manually checking each site. Keyword alerts are noisy,
so an AI filter ranks and removes junk. The same screen works as a lightweight tracker.

**Principles:** reliable (nothing silently lost), authentic (every row links to its source and keeps the original text),
simple (one process, one database file, no paid infrastructure).

**Non-goals:** multi-user, hosting on the internet, scraping sites without a legitimate access route.

## 2. Architecture
```
 F5Bot ─email─▶ Gmail label ─┐
 Hacker News API ────────────┤
 Stack Exchange API ─────────┼─▶ Ingest ─▶ Merge ─▶ Classify ─▶ SQLite ─▶ FastAPI ─▶ Dashboard
 GitHub API ─────────────────┤    (adapters)  (dedupe)  (rules + AI)           (JSON)     (browser)
 RSS · pasted links ─────────┘
              ▲
              └── scheduler thread (every N minutes) or "Refresh now"
```
A single Python process hosts the scheduler, the API and the static UI.

| Component | Responsibility |
|---|---|
| **Source adapters** | One module per source. Each returns the same item shape. A failing source is reported, never fatal to the others. |
| **Merge** | Insert-only upsert keyed by a hash of the canonical URL. A fetch never changes or removes an existing row. |
| **Classifier** | Rules first, then an AI model, then routing to Inbox or Junk. |
| **Engine** | Runs fetch, merge and classify as one cycle, records every run, takes a daily backup. |
| **API / UI** | Lists items per tab, applies tracker actions, exposes source health. |

## 3. Data model (SQLite, `data/foundersearch.db`)
The database is created automatically on first run; there is nothing to install or configure.

| Table | Purpose |
|---|---|
| `items` | One row per unique URL. Original content (title, url, snippet, author, community, dates, source) is stored unchanged. Also holds triage output (category, score, reason, method) and tracker state (`status`, `flag`, `note`). |
| `raw_emails` | Every F5Bot email as received, with parse status. Unparseable emails are kept and flagged, never dropped. |
| `source_runs` | One row per source per run: status, items fetched, items new, error text. Drives the health indicators. |
| `kv` | Cursors (last successful position per source) and the "last checked" dates for research links. |

`items.status` is one of `pending`, `inbox`, `selected`, `junk`, `deleted`. The tabs are views of this single column,
so moving an item between tabs changes one value and copies no data.

## 4. Ingestion and merge
- **Cursors** advance only after the fetched items are safely stored. Each run re-reads a short overlap window, and the
  merge ignores what it already has, so a crash or a missed run cannot create gaps.
- **Identity:** URLs are canonicalised (tracking parameters removed, Reddit hosts unified, trailing slash dropped).
- **Dismissed items** stay in the table with status `deleted`, so they are not re-fetched as new.
- **Email source:** Gmail over IMAP, reading only the `F5Bot` label, using the message UID as a cursor. Service
  messages (welcome, verification) are recognised and skipped. The parser extracts every link to a monitored site
  together with its surrounding text, which tolerates layout changes.
- **Limits:** if a source returns more than its page cap in one window, the dashboard says so instead of hiding it.

## 5. Classification
Runs once per new item, cheapest step first.

1. **Rules:** items with almost no text, or matching the configurable blocklist, go to Junk with no model call.
2. **AI judgement:** remaining items are sent in batches to an OpenAI-compatible model, together with a profile
   describing what counts as a good signal (`triage.profile` in `config.yaml`). The model returns a category
   (`problem`, `client`, `junk`), a score from 0 to 100 and a one-sentence reason that cites the evidence.
3. **Routing:** score at or above `triage.threshold` goes to Inbox, the rest to Junk (kept for audit and **Restore**).
4. **Feedback:** the most recent items you tracked or dismissed are included as examples in later prompts.
5. **Fallback and failure handling:** without a key, or after repeated failures, a transparent keyword scorer is used.
   On quota or outage errors the cycle stops calling the model and items stay `pending` for the next cycle.

The model only labels and summarises. It never rewrites the source text.

## 6. API
| Endpoint | Purpose |
|---|---|
| `GET /api/items?status=` | Items for a tab (`inbox`, `selected`, `junk`) |
| `POST /api/items/bulk` | Apply `select`, `dismiss`, `restore` or `untrack` to many items |
| `POST /api/items/{id}/select · delete · restore · flag · note` | Single-item actions |
| `POST /api/manual` · `GET /capture` | Add pasted links or use the bookmarklet |
| `POST /api/refresh` · `GET /api/status` | Trigger a cycle; counts, engine state and per-source health |
| `GET /api/research` · `POST /api/research/{i}/checked` | Manual search links and their last-checked dates |

## 7. Dashboard
- **Inbox:** Track, Dismiss. **Tracking:** In progress (row turns yellow), Remove (back to Inbox).
  **Junk:** Restore. **Research:** launcher for sources without a free API.
- Search, type, source and minimum-score filters, sortable columns, bulk actions, undo, an expandable row with the
  original text and a notes field, a "New" marker for items since your last visit, and light/dark themes.
- A source health strip shows each source as healthy, partial, failed, stale or not configured. A banner appears when
  data may be missing.

## 8. Reliability and security
- Insert-only merge, soft deletes, idempotent runs, per-source isolation, retry with backoff, run lock.
- Daily database backup (last 14 kept) and a rotating log.
- Binds to `127.0.0.1` only; no authentication by design. Secrets live in `.env` (git-ignored).
- Item text is sent to the configured AI provider; nothing else leaves the machine.
- 15 automated tests cover identity, merge idempotency, soft delete, classification routing, AI failure handling,
  engine behaviour on failing sources, the tracker flow and the email parser.

## 9. Known limitations and next steps
- The F5Bot parser is validated against a stand-in, not a real alert email. It is confirmed once the first alert
  arrives (`python run.py inspect-email`).
- Reddit coverage depends on F5Bot (5 keywords on the free plan) or an approved Reddit API app. X and review sites
  (G2, Capterra) have no free access and are handled through the Research tab.
- Next: clustering of repeated complaints with occurrence counts, and learning thresholds from tracking history.
