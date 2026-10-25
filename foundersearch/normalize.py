"""URL canonicalisation. The canonical form is the identity of an item: same URL, same row."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "gbraid", "wbraid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url",
    "share_id", "context", "ref", "ref_source", "correlation_id", "$deep_link", "rdt",
}
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.I)


def canonical_url(url: str) -> str:
    url = url.strip().rstrip(".,;:!?)")
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").lower()
    if host == "reddit.com" or host.endswith(".reddit.com"):
        host = "www.reddit.com"
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    query.sort()
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return f"https://{host}{path}" + (f"?{urlencode(query)}" if query else "")


def item_id(url: str) -> str:
    canon = canonical_url(url)
    key = canon.split("://", 1)[1]
    if key.startswith("www.") and not key.startswith("www.reddit.com"):
        key = key[4:]
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def unwrap_redirect(url: str) -> str:
    """Unwrap google.com/url?...&url=REAL style tracking redirects."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith("google.com") and parts.path.startswith("/url"):
        for k, v in parse_qsl(parts.query):
            if k in ("url", "q") and v.startswith("http"):
                return v
    return url


def community_from_url(url: str) -> str:
    m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", url)
    if m:
        return f"r/{m.group(1)}"
    host = (urlsplit(url).hostname or "").lower()
    if host == "news.ycombinator.com":
        return "Hacker News"
    if host.endswith("lobste.rs"):
        return "Lobsters"
    return ""
