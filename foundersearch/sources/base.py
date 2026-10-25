"""Source adapter base: every source returns the same FetchResult, and failures are reported, never swallowed."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .. import config
from ..db import Database
from ..models import FetchResult, iso, utcnow

USER_AGENT = "FounderSearch/0.1 (personal research tool)"


@dataclass
class Context:
    cfg: dict
    db: Database
    transport: httpx.BaseTransport | None = None  # injected in tests
    sleep: callable = time.sleep
    imap_factory: callable | None = None  # injected in tests

    def http(self, headers: dict | None = None, timeout: float = 30) -> httpx.Client:
        h = {"User-Agent": USER_AGENT}
        h.update(headers or {})
        return httpx.Client(headers=h, timeout=timeout, transport=self.transport, follow_redirects=True)

    def since(self, key: str) -> datetime:
        """Start of the window to read: last successful cursor minus an overlap, or the first-run lookback."""
        overlap = timedelta(minutes=config.get(self.cfg, "app.overlap_minutes", 180))
        raw = self.db.kv_get(f"cursor:{key}")
        if raw:
            return datetime.fromisoformat(raw) - overlap
        return utcnow() - timedelta(days=config.get(self.cfg, "app.first_run_lookback_days", 7))

    def now_cursor(self) -> str:
        return iso()

    def queries(self, source_cfg: dict | None = None) -> list[tuple[str, str]]:
        """(phrase, hint) pairs: the source's own list if given, otherwise the global lists."""
        if source_cfg and source_cfg.get("queries"):
            return [(q, "") for q in source_cfg["queries"]]
        out: list[tuple[str, str]] = []
        for hint in ("problem", "client"):
            out += [(q, hint) for q in config.get(self.cfg, f"queries.{hint}", [])]
        return out


class Source:
    name = "base"

    def __init__(self, ctx: Context, cfg: dict):
        self.ctx = ctx
        self.cfg = cfg

    def configured(self) -> tuple[bool, str]:
        """(ok, reason). A source that lacks credentials is skipped and shown as such, not silently ignored."""
        return True, ""

    def fetch(self) -> FetchResult:
        raise NotImplementedError


def get_json(client: httpx.Client, url: str, params: dict | None = None, headers: dict | None = None,
             retries: int = 3, sleep=time.sleep) -> dict | list:
    """GET with retry/backoff on network errors, 429 and 5xx. Raises with a readable message on failure."""
    last = ""
    for attempt in range(retries):
        try:
            r = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            last = f"network error: {exc}"
        else:
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
            retry_after = r.headers.get("Retry-After", "")
            if retry_after.isdigit():
                sleep(min(int(retry_after), 60))
                continue
        sleep(2 ** attempt)
    raise RuntimeError(last)


def strip_html(text: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def parse_dt(value: str | int | float | None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return iso(datetime.fromtimestamp(value, tz=timezone.utc))
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return iso(dt)
    except ValueError:
        return ""
