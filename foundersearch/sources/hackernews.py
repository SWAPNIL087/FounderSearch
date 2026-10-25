"""Hacker News via the official Algolia search API (free, no key)."""
from __future__ import annotations

from ..models import FetchResult, RawItem
from .base import Source, get_json, parse_dt, strip_html

API = "https://hn.algolia.com/api/v1/search_by_date"


class HackerNews(Source):
    name = "hackernews"

    def fetch(self) -> FetchResult:
        res = FetchResult()
        max_pages = int(self.cfg.get("max_pages", 5))
        pause = float(self.cfg.get("pause_seconds", 0.3))
        with self.ctx.http() as client:
            for phrase, hint in self.ctx.queries(self.cfg):
                started = self.ctx.now_cursor()
                since = int(self.ctx.since(f"hn:{phrase}").timestamp())
                try:
                    items, truncated = self._search(client, phrase, hint, since, max_pages)
                except RuntimeError as exc:
                    res.errors.append(f'"{phrase}": {exc}')
                    continue
                res.items += items
                res.cursors[f"cursor:hn:{phrase}"] = started
                if truncated:
                    res.errors.append(f'"{phrase}": more than {max_pages * 100} hits in the window; older ones skipped')
                self.ctx.sleep(pause)
        return res

    def _search(self, client, phrase, hint, since, max_pages):
        out: list[RawItem] = []
        page = 0
        while True:
            data = get_json(client, API, params={
                "query": f'"{phrase}"',
                "tags": "(story,comment)",
                "numericFilters": f"created_at_i>{since}",
                "hitsPerPage": 100,
                "page": page,
            }, sleep=self.ctx.sleep)
            for h in data.get("hits", []):
                out.append(self._to_item(h, phrase, hint))
            page += 1
            if page >= data.get("nbPages", 0):
                return out, False
            if page >= max_pages:
                return out, True

    def _to_item(self, h: dict, phrase: str, hint: str) -> RawItem:
        oid = h.get("objectID", "")
        is_comment = "comment" in (h.get("_tags") or [])
        if is_comment:
            title = "Comment on: " + (h.get("story_title") or "(untitled thread)")
            body = strip_html(h.get("comment_text") or "")
        else:
            title = h.get("title") or ""
            body = strip_html(h.get("story_text") or "") or (h.get("url") or "")
        return RawItem(
            source=self.name, url=f"https://news.ycombinator.com/item?id={oid}", title=title, snippet=body,
            author=h.get("author") or "", community="Hacker News",
            published_at=parse_dt(h.get("created_at_i") or h.get("created_at")), query=phrase, hint=hint,
        )
