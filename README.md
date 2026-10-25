# FounderSearch

A local dashboard that collects **problems people complain about** and **people looking for help** from F5Bot
alert emails, Hacker News, Stack Exchange and GitHub, filters out the junk with an AI model, and doubles as a tracker.
Everything runs on your machine; data lives in one SQLite file.

```
F5Bot ──email──▶ Gmail ─┐
Hacker News / Stack Exchange / GitHub ─┼─▶ merge (no duplicates) ─▶ AI filter ─▶ SQLite ─▶ API ─▶ dashboard
Links you paste / bookmarklet ─────────┘
```

## Quick setup (about 10 minutes, any machine)

Requires **Python 3.10+** and internet access. No database server needed.

```bash
git clone <your-repo-url> FounderSearch
cd FounderSearch
python setup.py          # creates .venv, installs packages, creates .env
```

Open `.env` and fill in the values from [Accounts and keys](#accounts-and-keys), then:

```bash
python setup.py          # re-run: checks Gmail, the AI key and every source, and tells you what to fix
python run.py            # starts the app, then open http://127.0.0.1:8765
```

Leave `python run.py` running (the background refresh only works while it runs). Stop it with `Ctrl+C`.

## Accounts and keys

You need two things. Both are free. **Keys go only in `.env`, which is never committed to git.**

### 1. Gmail account for F5Bot alerts (reads the alert emails)
Use a **dedicated** Gmail account so the app only ever sees F5Bot mail.

1. In that Gmail account: Settings → *See all settings* → **Filters and Blocked Addresses** → *Create a new filter*.
   - **From:** `f5bot` → *Create filter* → tick **Apply the label** → *New label* → name it exactly **`F5Bot`**
     → tick **Never send it to Spam** → *Create filter*.
2. Turn on **2-Step Verification**: myaccount.google.com/security.
3. Create an **app password**: myaccount.google.com/apppasswords → name it `FounderSearch` → *Create*.
   Google shows 16 characters once. Copy them.
4. In `.env`:
   ```
   IMAP_USER=your.alert.account@gmail.com
   IMAP_PASSWORD=abcdefghijklmnop
   ```
   (`IMAP_HOST=imap.gmail.com` is already set. No quotes, no spaces around `=`, no comment after the value.)

### 2. AI filter key (Google Gemini, free tier)
1. Go to aistudio.google.com/apikey → *Create API key*.
2. In `.env`:
   ```
   LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
   LLM_API_KEY=AIza...
   LLM_MODEL=gemini-3.5-flash-lite
   ```
   - Use a **lite** model. The free tier gives `gemini-3.5-flash` only about 20 requests per day, which runs out
     quickly. If a model name is rejected, run `python run.py doctor`, or list the available names in AI Studio.
   - Free-tier requests may be used by Google to improve its models, so don't put private data in `config.yaml`'s profile.
   - Without a key the app still works using a simple keyword scorer, but it is noticeably noisier.
   - Any OpenAI-compatible service works (set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). For Azure OpenAI use the
     deployment URL as `LLM_BASE_URL` and set `LLM_API_VERSION`.

### 3. F5Bot alerts (Reddit comments and posts)
Sign up at f5bot.com with the **alert Gmail address**, verify it, and add keywords in the Dashboard.

**Important:** F5Bot matches what you type **literally**, including quote characters. **Do not wrap phrases in
quotes.** Put the options (flags) in the flags field, not in the phrase.

The free plan allows **5 keywords**. A keyword that goes over its daily limit is switched off, so use rare, specific
phrases. Suggested set (add the flags `whole` and `only-reddit` to each):

| # | Phrase (type exactly this, no quotes) | Finds |
|---|---|---|
| 1 | `wish there was a tool` | unmet needs |
| 2 | `willing to pay for` | buying intent |
| 3 | `cancelled my subscription` | complaints about paid products |
| 4 | `looking for a developer` | client leads |
| 5 | `looking for an alternative to` | complaints with switching intent |

Rules: don't create more accounts to get more free keywords (F5Bot forbids it). Rotate a keyword every week or two if
it brings junk. Hacker News is already covered by the app's own fetcher, which is why `only-reddit` saves your daily quota.
Free alerts can arrive up to 2 hours late. Silver ($12.99/mo) adds 20 keywords and 5-minute delivery.

### Optional keys (all in `.env`)
- `GITHUB_TOKEN`: raises the GitHub search limit. Create one at github.com/settings/tokens (no scopes needed).
- `STACKEXCHANGE_KEY`: raises the Stack Exchange quota (stackapps.com/apps/oauth/register).
- `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`: only if Reddit approves an API app for you; then set
  `sources.reddit_api.enabled: true` in `config.yaml`.

## Using the dashboard

| Tab | What it is | Actions |
|---|---|---|
| **Inbox** | New items the filter judged relevant, best first | **Track** (move to Tracking), **Dismiss** (hide for good) |
| **Tracking** | Things you are following | **In progress** (row turns yellow), **Remove** (back to Inbox) |
| **Junk** | What the filter rejected, kept so you can audit it | **Restore** |
| **Research** | One-click searches for places with no free API (Reddit, X, G2, Capterra), with a "last checked" date | Open, Mark checked |

- Click a title to read the original text, add your own note, or open the source link.
- **Refresh now** fetches immediately. The app also refreshes by itself every 30 minutes.
- **+ Add link** or the bookmarklet (drag the button on the Research tab to your bookmarks bar) saves any page you find
  by hand straight to your Inbox.
- Tick several rows to Track or Dismiss in bulk. Every action has an **Undo**.
- The coloured dots at the top show each source's health. A red dot or banner means data may be missing.
- Existing rows are never overwritten by a refresh, so your tracking, colours and notes persist.

## Tune the filter (most important setting)
Edit `triage.profile` in `config.yaml`. Describe in plain words who you are and what counts as a good problem or client.
The better this text, the better the filter. Also adjust:
- `triage.threshold` (default 40): raise it (e.g. 55) for a stricter Inbox.
- `queries`: the search phrases used for Hacker News and GitHub.
- `sources.*.enabled`: turn a source on or off.

After editing the profile, run `python run.py rescore --days 7` to re-judge recent items.

## Commands
```bash
python run.py                 # start the app (default)
python run.py doctor          # check Gmail, AI key and every source
python run.py fetch           # one fetch + classify, no UI
python run.py inspect-email   # show how stored F5Bot emails were parsed
python run.py reparse         # re-parse emails that failed (after a parser fix)
python run.py rescore --days 7
python run.py export items.csv
python run.py backup
python -m pytest tests -q     # run the tests
```

## Where things are stored
- Database: `data/foundersearch.db` (SQLite, created automatically on first run; tables: `items`, `raw_emails`,
  `source_runs`, `kv`). Daily backups go to `data/backups/` (last 14 kept). Logs: `data/foundersearch.log`.
- To move to another machine with your history, copy `data/foundersearch.db`. Copy `.env` separately and securely
  (never through git). A new machine can also start empty: the first run reads the last 7 days.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `python run.py doctor` says Gmail login failed | Use the 16-character **app password**, not your Google password; 2-Step Verification must be on |
| "label/folder 'F5Bot' not found" | Create the Gmail label and filter (step 1 above); the label must be named exactly `F5Bot` |
| No Reddit items | F5Bot only emails after a keyword matches; free alerts can lag up to 2 hours. Check the F5Bot dashboard for disabled keywords, and that phrases have no quotes |
| AI errors `429` | Free quota used up. Use a `-lite` model, wait a day, or add billing. Items stay pending and are retried; nothing is lost |
| AI errors `503` | Model overloaded. Items retry automatically on the next cycle |
| Too much junk in the Inbox | Improve `triage.profile`, raise `triage.threshold`, then `rescore` |
| Port in use | Change `app.port` in `config.yaml` |

## Security
The app listens on `127.0.0.1` only and has no login. Don't expose it to a network. `.env` holds secrets and is
git-ignored. Item text is sent to your AI provider for classification; nothing else leaves your machine.
