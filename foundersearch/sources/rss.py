"""Generic RSS/Atom feeds (Google Alerts, blogs, forums...)."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

import feedparser

from ..models import FetchResult, RawItem, iso
from ..normalize import unwrap_redirect
from .base import USER_AGENT, Source, strip_html


class RSS(Source):
    name = "rss"

    def fetch(self) -> FetchResult:
        res = FetchResult()
        with self.ctx.http() as client:
            for feed in self.cfg.get("feeds") or []:
                name, url = feed.get("name", feed.get("url", "")), feed.get("url", "")
                if not url:
                    continue
                try:
                    r = client.get(url)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    parsed = feedparser.parse(r.content)
                    if parsed.bozo and not parsed.entries:
                        raise RuntimeError(f"not a valid feed ({parsed.bozo_exception})")
                except Exception as exc:  # noqa: BLE001 - reported per feed, other feeds continue
                    res.errors.append(f"{name}: {exc}")
                    continue
                for e in parsed.entries:
                    link = unwrap_redirect(e.get("link", ""))
                    ts = e.get("published_parsed") or e.get("updated_parsed")
                    published = iso(datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)) if ts else ""
                    res.items.append(RawItem(
                        source=self.name, url=link, title=strip_html(e.get("title", "")),
                        snippet=strip_html(e.get("summary", "")), author=e.get("author", ""),
                        community=name, published_at=published, query=name, hint=feed.get("hint", "")))
        return res
