"""Stack Exchange: every new question on chosen sites (softwarerecs, webapps, ...). Official API."""
from __future__ import annotations

import html

from .. import config
from ..models import FetchResult, RawItem
from .base import Source, get_json, parse_dt, strip_html

API = "https://api.stackexchange.com/2.3/questions"


class StackExchange(Source):
    name = "stackexchange"

    def fetch(self) -> FetchResult:
        res = FetchResult()
        key = config.env("STACKEXCHANGE_KEY")
        max_pages = int(self.cfg.get("max_pages", 3))
        with self.ctx.http() as client:
            for site in self.cfg.get("sites", []):
                started = self.ctx.now_cursor()
                since = int(self.ctx.since(f"se:{site}").timestamp())
                try:
                    items = self._site(client, site, since, max_pages, key, res)
                except RuntimeError as exc:
                    res.errors.append(f"{site}: {exc}")
                    continue
                res.items += items
                res.cursors[f"cursor:se:{site}"] = started
        return res

    def _site(self, client, site, since, max_pages, key, res):
        out = []
        for page in range(1, max_pages + 1):
            params = {"order": "desc", "sort": "creation", "site": site, "fromdate": since,
                      "pagesize": 100, "page": page, "filter": "withbody"}
            if key:
                params["key"] = key
            data = get_json(client, API, params=params, sleep=self.ctx.sleep)
            if data.get("backoff"):
                self.ctx.sleep(min(int(data["backoff"]), 30))
            for q in data.get("items", []):
                out.append(RawItem(
                    source=self.name, url=q.get("link", ""), title=html.unescape(q.get("title", "")),
                    snippet=strip_html(q.get("body", "")), author=(q.get("owner") or {}).get("display_name", ""),
                    community=f"{site}.stackexchange", published_at=parse_dt(q.get("creation_date")),
                    query=site, hint="problem"))
            if data.get("quota_remaining", 1000) < 20:
                res.errors.append(f"Stack Exchange quota nearly exhausted ({data['quota_remaining']} left); add STACKEXCHANGE_KEY")
                break
            if not data.get("has_more"):
                break
        else:
            res.errors.append(f"{site}: more than {max_pages * 100} new questions in the window; older ones skipped")
        return out
