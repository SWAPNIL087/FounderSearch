"""Turn an F5Bot alert email into items.

The parser is deliberately layout-tolerant: it finds every link to a monitored site (Reddit, Hacker News,
Lobsters) and takes the surrounding text as the snippet, so it survives small layout changes. If nothing can
be extracted the email is kept raw and flagged - it is never dropped. Run `python -m foundersearch inspect-email`
to see how a stored email is parsed.
"""
from __future__ import annotations

import email
import re
from email import policy
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup

from ..models import RawItem, iso
from ..normalize import URL_RE, canonical_url, community_from_url, unwrap_redirect

SITE_HOSTS = ("reddit.com", "redd.it", "news.ycombinator.com", "lobste.rs")
GENERIC_TEXT = {"view", "view on reddit", "link", "here", "reddit", "hacker news", "read more", "permalink",
                "comments", "context", "view comment", "view post", "open", "source", "view thread", "lobsters"}
BOILERPLATE = re.compile(r"(unsubscribe|manage your alerts|f5bot|you are receiving this|upgrade|advertis)", re.I)
SUBJECT_KEYWORD = re.compile(r"""['"“‘](.+?)['"”’]""")


def _is_site(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SITE_HOSTS)


def _resolve(href: str) -> str | None:
    """Return the monitored-site URL behind href, unwrapping tracking redirects. None if not a monitored link."""
    href = unwrap_redirect(href.strip())
    if _is_site(href):
        return href
    parts = urlsplit(href)
    for _, v in parse_qsl(parts.query):
        if v.startswith("http") and _is_site(v):
            return v
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def message_parts(raw: bytes) -> tuple[email.message.EmailMessage, str, str]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    html = text = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/html" and not html:
            html = part.get_content()
        elif ctype == "text/plain" and not text:
            text = part.get_content()
    return msg, html, text


def parse_email(raw: bytes) -> tuple[list[RawItem], dict]:
    """Returns (items, meta). meta: subject, message_id, received_at, keyword."""
    msg, html, text = message_parts(raw)
    subject = _clean(str(msg.get("Subject", "")))
    m = SUBJECT_KEYWORD.search(subject)
    keyword = m.group(1) if m else subject
    received = ""
    try:
        received = iso(email.utils.parsedate_to_datetime(str(msg.get("Date"))))
    except (TypeError, ValueError):
        pass
    meta = {"subject": subject, "message_id": str(msg.get("Message-ID", "")), "received_at": received,
            "keyword": keyword}
    items = _from_html(html) if html else []
    if not items and text:
        items = _from_text(text)
    for it in items:
        it.source, it.query, it.published_at = "f5bot", keyword, received
        it.community = it.community or community_from_url(it.url)
    return items, meta


def _from_html(html: str) -> list[RawItem]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = []
    for a in soup.find_all("a", href=True):
        url = _resolve(a["href"])
        if url:
            anchors.append((a, url))
    if not anchors:
        return []
    by_url: dict[str, dict] = {}
    for a, url in anchors:
        key = canonical_url(url)
        entry = by_url.setdefault(key, {"url": url, "titles": [], "block": None})
        label = _clean(a.get_text(" "))
        if label and label.lower() not in GENERIC_TEXT and len(label) > 3:
            entry["titles"].append(label)
        if entry["block"] is None:
            entry["block"] = _block_text(a, set(by_url), anchors)
    items = []
    for entry in by_url.values():
        block = entry["block"] or ""
        title = max(entry["titles"], key=len) if entry["titles"] else ""
        snippet = block
        if title and snippet.startswith(title):
            snippet = snippet[len(title):].strip(" -–—:|")
        if not title:
            title, snippet = _split_first_sentence(block)
        snippet = _drop_boilerplate(snippet)
        author = ""
        am = re.search(r"\bu/([A-Za-z0-9_\-]{2,})", block)
        if am:
            author = am.group(1)
        items.append(RawItem(source="f5bot", url=entry["url"], title=title[:300], snippet=snippet[:2000],
                             author=author))
    return items


def _block_text(anchor, _seen, anchors) -> str:
    """Text of the largest ancestor that contains exactly one distinct monitored URL."""
    urls_by_node = {id(a): canonical_url(u) for a, u in anchors}
    mine = urls_by_node[id(anchor)]
    best, node = anchor, anchor.parent
    while node is not None and getattr(node, "name", None) not in (None, "body", "html", "[document]"):
        inside = {urls_by_node[id(a)] for a, _ in anchors if _contains(node, a)}
        if inside != {mine}:
            break
        best, node = node, node.parent
    return _clean(best.get_text(" "))


def _contains(node, a) -> bool:
    return any(p is node for p in a.parents)


def _split_first_sentence(block: str) -> tuple[str, str]:
    block = _clean(block)
    m = re.match(r"(.{10,140}?[.!?])\s+(.*)", block)
    if m:
        return m.group(1), m.group(2)
    return block[:140], block[140:]


def _drop_boilerplate(text: str) -> str:
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if not BOILERPLATE.search(p)]
    return _clean(" ".join(parts))


def _from_text(text: str) -> list[RawItem]:
    items, seen = [], set()
    for para in re.split(r"\n\s*\n", text):
        for url in URL_RE.findall(para):
            real = _resolve(url)
            if not real or canonical_url(real) in seen:
                continue
            seen.add(canonical_url(real))
            lines = [ln.strip() for ln in para.splitlines() if ln.strip() and url not in ln]
            lines = [ln for ln in lines if not BOILERPLATE.search(ln)]
            title = lines[0] if lines else real
            items.append(RawItem(source="f5bot", url=real, title=title[:300], snippet=_clean(" ".join(lines[1:]))[:2000]))
    return items
