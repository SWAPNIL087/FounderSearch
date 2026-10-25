"""`python -m foundersearch doctor`: verifies every connection and tells you exactly what to fix."""
from __future__ import annotations

import imaplib

import httpx

from . import config
from .db import Database
from .sources.base import USER_AGENT
from .triage.llm import LLMClient, LLMError

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _say(level: str, name: str, msg: str, fix: str = "") -> None:
    print(f"[{level}] {name}: {msg}")
    if fix:
        print(f"       -> {fix}")


def _imap_check(cfg: dict):
    scfg = config.get(cfg, "sources.f5bot_email", {})
    if not scfg.get("enabled"):
        return WARN, "disabled in config.yaml", ""
    if not (config.env("IMAP_USER") and config.env("IMAP_PASSWORD")):
        return FAIL, "IMAP_USER / IMAP_PASSWORD not set", "SETUP.md step 3 (Gmail app password)."
    try:
        conn = imaplib.IMAP4_SSL(config.env("IMAP_HOST", "imap.gmail.com"), timeout=30)
        conn.login(config.env("IMAP_USER"), config.env("IMAP_PASSWORD"))
    except Exception as exc:  # noqa: BLE001
        return (FAIL, f"login failed ({exc})",
                "Use a 16-char app password (needs 2-Step Verification), not your Google password.")
    try:
        folder = scfg.get("folder", "INBOX")
        typ, _ = conn.select(f'"{folder}"' if " " in folder else folder, readonly=True)
        if typ != "OK":
            return FAIL, f"label/folder '{folder}' not found", "Create the Gmail label and filter (SETUP.md step 3)."
        typ, data = conn.uid("SEARCH", None, "FROM", f'"{scfg.get("sender", "f5bot")}"')
        n = len(data[0].split()) if typ == "OK" and data[0] else 0
        if n == 0:
            return (WARN, f"connected; '{folder}' has no emails from '{scfg.get('sender')}' yet",
                    "Normal until F5Bot sends its first alert. Add keywords at f5bot.com (SETUP.md step 4).")
        return PASS, f"connected; {n} F5Bot emails in '{folder}'", ""
    finally:
        conn.logout()


def _llm_check():
    client = LLMClient.from_env()
    if client is None:
        return (WARN, "no LLM_API_KEY: the keyword scorer will be used (works, but noisier)",
                "Add an OpenAI-compatible key in .env (SETUP.md step 5).")
    try:
        out = client.complete_json('Reply with JSON only: {"ok": true}', "ping")
    except LLMError as exc:
        return FAIL, f"call failed: {exc}", "Check LLM_BASE_URL, LLM_API_KEY and LLM_MODEL."
    return PASS, f"{client.model} replied ({out})", ""


def _get(url, params=None, headers=None):
    with httpx.Client(headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=20) as c:
        return c.get(url, params=params)


def _hn_check():
    r = _get("https://hn.algolia.com/api/v1/search_by_date", {"query": '"alternative to"', "hitsPerPage": 1})
    return (PASS, "reachable", "") if r.status_code == 200 else (FAIL, f"HTTP {r.status_code}", "")


def _se_check():
    r = _get("https://api.stackexchange.com/2.3/questions", {"site": "softwarerecs", "pagesize": 1})
    if r.status_code != 200:
        return FAIL, f"HTTP {r.status_code}", ""
    return PASS, f"reachable (quota left: {r.json().get('quota_remaining')})", ""


def _gh_check():
    tok = config.env("GITHUB_TOKEN")
    r = _get("https://api.github.com/rate_limit", headers={"Authorization": f"Bearer {tok}"} if tok else {})
    if r.status_code != 200:
        return FAIL, f"HTTP {r.status_code}", "Check GITHUB_TOKEN."
    left = r.json()["resources"]["search"]["remaining"]
    hint = "" if tok else "; add GITHUB_TOKEN for a higher limit"
    return PASS, f"reachable (search calls left this minute: {left}{hint})", ""


def _reddit_check(cfg: dict):
    scfg = config.get(cfg, "sources.reddit_api", {})
    if not scfg.get("enabled"):
        return WARN, "disabled (optional)", ""
    if not (config.env("REDDIT_CLIENT_ID") and config.env("REDDIT_CLIENT_SECRET")):
        return FAIL, "enabled but REDDIT_CLIENT_ID/SECRET are empty", "SETUP.md step 7, or set enabled: false."
    with httpx.Client(headers={"User-Agent": config.env("REDDIT_USER_AGENT", USER_AGENT)}, timeout=20) as c:
        r = c.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                   auth=(config.env("REDDIT_CLIENT_ID"), config.env("REDDIT_CLIENT_SECRET")))
    if r.status_code == 200 and "access_token" in r.text:
        return PASS, "token granted", ""
    return FAIL, f"token request failed (HTTP {r.status_code})", "Check the app type is 'script' and the keys are right."


def run_doctor(cfg: dict, db: Database) -> int:
    failures = 0

    def check(name, fn):
        nonlocal failures
        try:
            level, msg, fix = fn()
        except Exception as exc:  # noqa: BLE001
            level, msg, fix = FAIL, f"{type(exc).__name__}: {exc}", ""
        failures += level == FAIL
        _say(level, name, msg, fix)

    check("Config", lambda: (PASS, f"config.yaml loaded; database at {db.path}", ""))
    check(".env", lambda: (PASS, "found", "") if (config.ROOT / ".env").exists() else
          (FAIL, "missing", "Run `python setup.py`, or copy .env.example to .env and fill it in."))
    check("Gmail / F5Bot mailbox", lambda: _imap_check(cfg))
    check("LLM", _llm_check)
    for key, name, fn in (("hackernews", "Hacker News API", _hn_check), ("stackexchange", "Stack Exchange API", _se_check),
                          ("github", "GitHub API", _gh_check)):
        if config.get(cfg, f"sources.{key}.enabled"):
            check(name, fn)
    check("Reddit API", lambda: _reddit_check(cfg))

    print()
    print("All required checks passed." if not failures else f"{failures} check(s) failed - fix the items marked FAIL.")
    return 1 if failures else 0
