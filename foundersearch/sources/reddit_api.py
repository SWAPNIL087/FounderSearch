"""Optional: Reddit official API (app-only OAuth). Needs a 'script' app that Reddit has approved for you.

Search covers posts only (the API has no comment search); F5Bot emails cover comments.
"""
from __future__ import annotations

import httpx

from .. import config
from ..models import FetchResult, RawItem
from .base import Source, get_json, parse_dt

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"


class RedditAPI(Source):
    name = "reddit_api"

    def configured(self):
        if not (config.env("REDDIT_CLIENT_ID") and config.env("REDDIT_CLIENT_SECRET")):
            return False, "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set in .env"
        return True, ""

    def _token(self, client: httpx.Client) -> str:
        r = client.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                        auth=(config.env("REDDIT_CLIENT_ID"), config.env("REDDIT_CLIENT_SECRET")))
        if r.status_code != 200 or "access_token" not in r.json():
            raise RuntimeError(f"Reddit token request failed: HTTP {r.status_code} {r.text[:150]}")
        return r.json()["access_token"]

    def fetch(self) -> FetchResult:
        res = FetchResult()
        ua = config.env("REDDIT_USER_AGENT", "foundersearch/0.1")
        max_pages = int(self.cfg.get("max_pages", 2))
        subs = self.cfg.get("subreddits") or [None]
        with self.ctx.http({"User-Agent": ua}) as client:
            try:
                token = self._token(client)
            except (RuntimeError, httpx.HTTPError) as exc:
                res.errors.append(str(exc))
                return res
            headers = {"Authorization": f"Bearer {token}"}
            for phrase, hint in self.ctx.queries(self.cfg):
                started = self.ctx.now_cursor()
                since = self.ctx.since(f"reddit:{phrase}").timestamp()
                try:
                    for sub in subs:
                        res.items += self._search(client, headers, phrase, hint, sub, since, max_pages)
                except RuntimeError as exc:
                    res.errors.append(f'"{phrase}": {exc}')
                    continue
                res.cursors[f"cursor:reddit:{phrase}"] = started
                self.ctx.sleep(1.0)
        return res

    def _search(self, client, headers, phrase, hint, sub, since, max_pages):
        out, after = [], None
        base = f"{API}/r/{sub}/search" if sub else f"{API}/search"
        for _ in range(max_pages):
            params = {"q": f'"{phrase}"', "sort": "new", "t": "month", "limit": 100, "type": "link", "raw_json": 1}
            if sub:
                params["restrict_sr"] = 1
            if after:
                params["after"] = after
            data = get_json(client, base, params=params, headers=headers, sleep=self.ctx.sleep)["data"]
            for c in data.get("children", []):
                d = c["data"]
                if d.get("created_utc", 0) < since:
                    return out
                out.append(RawItem(
                    source=self.name, url="https://www.reddit.com" + d.get("permalink", ""), title=d.get("title", ""),
                    snippet=d.get("selftext", "")[:1500], author=d.get("author", ""),
                    community=f"r/{d.get('subreddit', '')}", published_at=parse_dt(d.get("created_utc")),
                    query=phrase, hint=hint))
            after = data.get("after")
            if not after:
                break
        return out
