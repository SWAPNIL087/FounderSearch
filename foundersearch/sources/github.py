"""GitHub issue search (official REST API). Unauthenticated search is limited to ~10 requests/minute, so we pace."""
from __future__ import annotations

from .. import config
from ..models import FetchResult, RawItem
from .base import Source, get_json, parse_dt

API = "https://api.github.com/search/issues"


class GitHub(Source):
    name = "github"

    def fetch(self) -> FetchResult:
        res = FetchResult()
        token = config.env("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        pace = 2.5 if token else 7.0
        max_pages = int(self.cfg.get("max_pages", 2))
        with self.ctx.http(headers) as client:
            for phrase, _ in self.ctx.queries(self.cfg):
                started = self.ctx.now_cursor()
                since = self.ctx.since(f"gh:{phrase}").strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    items = self._search(client, phrase, since, max_pages, pace)
                except RuntimeError as exc:
                    res.errors.append(f'"{phrase}": {exc}')
                    continue
                res.items += items
                res.cursors[f"cursor:gh:{phrase}"] = started
        return res

    def _search(self, client, phrase, since, max_pages, pace):
        out = []
        for page in range(1, max_pages + 1):
            data = get_json(client, API, params={
                "q": f'"{phrase}" is:issue created:>={since}', "sort": "created", "order": "desc",
                "per_page": 50, "page": page}, sleep=self.ctx.sleep)
            for i in data.get("items", []):
                repo = i.get("repository_url", "").replace("https://api.github.com/repos/", "")
                out.append(RawItem(
                    source=self.name, url=i.get("html_url", ""), title=i.get("title", ""),
                    snippet=(i.get("body") or "")[:1500], author=(i.get("user") or {}).get("login", ""),
                    community=repo, published_at=parse_dt(i.get("created_at")), query=phrase, hint="problem"))
            self.ctx.sleep(pace)
            if len(data.get("items", [])) < 50:
                break
        return out
