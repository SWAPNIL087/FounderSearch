import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from foundersearch import config
from foundersearch.api import create_app
from foundersearch.db import Database
from foundersearch.engine import Engine
from foundersearch.models import RawItem
from foundersearch.normalize import canonical_url, item_id
from foundersearch.sources.f5bot_parser import parse_email
from foundersearch.triage.llm import LLMClient
from foundersearch.triage.pipeline import Triager


@pytest.fixture()
def cfg():
    c = config.load_config()
    c = copy.deepcopy(c)
    for name, s in c["sources"].items():
        s["enabled"] = name == "hackernews"
    c["queries"] = {"problem": ["alternative to"], "client": []}
    c["sources"]["hackernews"]["pause_seconds"] = 0
    return c


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "t.db")


def item(url="https://example.com/a", title="I am frustrated with pricing, is there an alternative to Notion",
         snippet="We pay $50 per month and it is too expensive for our team", **kw):
    return RawItem(source="hackernews", url=url, title=title, snippet=snippet, **kw)


# ---------- identity ----------
def test_url_variants_are_the_same_item():
    a = "https://old.reddit.com/r/SaaS/comments/abc/x/?utm_source=share&utm_medium=web"
    b = "https://www.reddit.com/r/SaaS/comments/abc/x"
    assert item_id(a) == item_id(b)
    assert canonical_url(a) == b


# ---------- merge ----------
def test_merge_is_insert_only_and_idempotent(db):
    assert db.insert_items([item(), item(url="https://example.com/b")]) == 2
    assert db.insert_items([item(), item(url="https://example.com/b")]) == 0
    iid = item_id("https://example.com/a")
    db.apply_triage(iid, "problem", 80, "r", "llm", "inbox")
    db.set_status(iid, "selected")
    db.toggle_flag(iid)
    db.insert_items([item(title="changed upstream")])  # re-fetch of the same URL
    row = db.get_item(iid)
    assert (row["status"], row["flag"], row["title"]) == ("selected", "yellow", item().title)


def test_dismissed_item_does_not_come_back(db):
    db.insert_items([item()])
    iid = item_id("https://example.com/a")
    db.set_status(iid, "deleted")
    assert db.insert_items([item()]) == 0
    assert db.counts()["deleted"] == 1 and db.counts()["pending"] == 0


def test_manual_add_overrides_junk(db):
    db.insert_items([item()])
    iid = item_id("https://example.com/a")
    db.apply_triage(iid, "junk", 5, "no", "heuristic", "junk")
    _, new = db.add_manual("https://example.com/a")
    assert new is False and db.get_item(iid)["status"] == "inbox"


# ---------- triage ----------
def llm_with(handler):
    return LLMClient("https://llm.test/v1", "k", "m", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def llm_reply(results):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"results": results})}}]})
    return handler


def test_triage_without_llm_uses_keyword_scorer(cfg, db):
    db.insert_items([item(), item(url="https://example.com/b", title="Nice weather", snippet="It is sunny outside today")])
    stats = Triager(cfg, db, None).run()
    assert stats["inbox"] == 1 and stats["junk"] == 1 and stats["pending"] == 0


def test_triage_with_llm_routes_by_score_and_stores_reason(cfg, db):
    db.insert_items([item(), item(url="https://example.com/b")])
    llm = llm_with(llm_reply([{"i": 0, "category": "problem", "score": 88, "reason": "pays $50 and complains"},
                              {"i": 1, "category": "junk", "score": 10, "reason": "promo"}]))
    Triager(cfg, db, llm).run()
    a, b = db.get_item(item_id("https://example.com/a")), db.get_item(item_id("https://example.com/b"))
    assert (a["status"], a["method"], a["reason"]) == ("inbox", "llm", "pays $50 and complains")
    assert b["status"] == "junk"


def test_llm_failure_never_drops_items(cfg, db):
    db.insert_items([item()])
    bad = llm_with(lambda r: httpx.Response(503, text="overloaded"))
    t = Triager(cfg, db, bad)
    stats = t.run()
    assert stats["pending"] == 1 and "503" in stats["llm_error"]
    assert db.get_item(item_id("https://example.com/a"))["status"] == "pending"
    for _ in range(cfg["triage"]["max_attempts"]):
        t.run()
    assert db.get_item(item_id("https://example.com/a"))["status"] in ("inbox", "junk")  # keyword fallback took over


def test_blocklist_and_short_text_skip_the_model(cfg, db):
    db.insert_items([item(title="Crypto airdrop now", snippet="claim your crypto airdrop"), item(url="https://x.com/s", title="hi", snippet="")])
    calls = []
    llm = llm_with(lambda r: calls.append(1) or httpx.Response(500))
    stats = Triager(cfg, db, llm).run()
    assert stats["junk"] == 2 and not calls


# ---------- engine ----------
def hn_transport(hits, status=200):
    def handler(request):
        if status != 200:
            return httpx.Response(status, text="boom")
        return httpx.Response(200, json={"hits": hits, "nbPages": 1})
    return httpx.MockTransport(handler)


HN_HIT = {"objectID": "42", "title": "Ask HN: alternative to Notion? too expensive", "story_text": "we pay $50 per month",
          "created_at_i": 1792900000, "author": "bob", "_tags": ["story"]}


def test_engine_merges_without_duplicates_and_keeps_old_data(cfg, db):
    eng = Engine(cfg, db, None, transport=hn_transport([HN_HIT]), sleep=lambda s: None)
    assert eng.run_cycle(force=True)["new"] == 1
    eng.transport = None
    eng2 = Engine(cfg, db, None, transport=hn_transport([]), sleep=lambda s: None)  # next fetch returns nothing
    assert eng2.run_cycle(force=True)["new"] == 0
    eng3 = Engine(cfg, db, None, transport=hn_transport([HN_HIT]), sleep=lambda s: None)  # same item again
    assert eng3.run_cycle(force=True)["new"] == 0
    assert db.counts()["inbox"] + db.counts()["junk"] == 1  # still exactly one row, nothing lost


def test_failed_source_is_reported_and_cursor_not_advanced(cfg, db):
    eng = Engine(cfg, db, None, transport=hn_transport([], status=500), sleep=lambda s: None)
    eng.run_cycle(force=True)
    health = {h["name"]: h for h in eng.health()}
    assert health["hackernews"]["state"] == "failed" and "500" in health["hackernews"]["detail"]
    assert db.kv_get("cursor:hn:alternative to") == ""  # next run re-reads the same window


# ---------- API / tracker ----------
@pytest.fixture()
def client(cfg, db):
    eng = Engine(cfg, db, None, transport=hn_transport([]), sleep=lambda s: None)
    db.insert_items([item(), item(url="https://example.com/b", title="Another need", snippet="looking for a developer to build a dashboard")])
    Triager(cfg, db, None).run()
    return TestClient(create_app(cfg, db, eng, start_scheduler=False)), db


def ids(c, status):
    return {i["id"] for i in c.get(f"/api/items?status={status}").json()["items"]}


def test_tracker_flow(client):
    c, db = client
    a = item_id("https://example.com/a")
    assert a in ids(c, "inbox")
    c.post(f"/api/items/{a}/select")
    assert a in ids(c, "selected") and a not in ids(c, "inbox")
    assert c.post(f"/api/items/{a}/flag").json()["flag"] == "yellow"
    assert c.get("/api/items?status=selected").json()["items"][0]["flag"] == "yellow"
    c.post(f"/api/items/{a}/delete")  # delete in Selected -> back to Inbox, colour cleared
    row = c.get("/api/items?status=inbox").json()["items"]
    assert a in {r["id"] for r in row} and next(r for r in row if r["id"] == a)["flag"] == ""


def test_dismiss_restore_bulk_and_note(client):
    c, _ = client
    a, b = item_id("https://example.com/a"), item_id("https://example.com/b")
    c.post("/api/items/bulk", json={"ids": [a, b], "action": "dismiss"})
    assert not ids(c, "inbox") & {a, b}
    c.post("/api/items/bulk", json={"ids": [a], "action": "restore"})
    assert a in ids(c, "inbox")
    assert c.post(f"/api/items/{a}/note", json={"note": "email Bob"}).status_code == 200
    assert c.get("/api/items?status=inbox").json()["items"][0]["note"] == "email Bob"
    assert c.post("/api/items/bulk", json={"ids": [a], "action": "nope"}).status_code == 400
    assert c.post("/api/items/doesnotexist/select").status_code == 404


def test_manual_add_and_status(client):
    c, _ = client
    r = c.post("/api/manual", json={"text": "see https://www.reddit.com/r/SaaS/comments/zzz/title/ thanks"}).json()
    assert r["added"] == 1
    assert c.post("/api/manual", json={"text": "no links here"}).status_code == 400
    st = c.get("/api/status").json()
    assert st["counts"]["inbox"] >= 2 and any(s["name"] == "hackernews" for s in st["sources"])


# ---------- F5Bot email parser (SYNTHETIC stand-in; replace with a real alert sample) ----------
SYNTHETIC_ALERT = b"""From: F5Bot <admin@f5bot.com>
Subject: [F5Bot] 2 new mentions of "wish there was a tool"
Date: Sun, 25 Oct 2026 13:14:42 +0000
Message-ID: <abc@f5bot.com>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body>
<div class="f5-hit"><div class="f5-hit-heading"><a href="https://www.reddit.com/r/SaaS/comments/abc123/need_better_invoicing/?utm_source=f5">Need better invoicing</a></div>
<div class="f5-meta">Reddit post in r/SaaS</div><div class="f5-excerpt">I wish there was a tool that does recurring invoices without the $60 per month fee.</div></div>
<div class="f5-hit"><div class="f5-hit-heading"><a href="https://news.ycombinator.com/item?id=777">Show HN: my crm</a></div>
<div class="f5-meta">Hacker News comment</div><div class="f5-excerpt">Honestly I wish there was a tool for follow ups.</div></div>
<div class="f5-ad"><a href="https://some-advertiser.example/offer">Buy our thing</a></div>
<div class="f5-footer"><a href="https://f5bot.com/unsubscribe?t=XXXX">Unsubscribe</a></div>
</body></html>"""


def test_parser_extracts_hits_and_ignores_ads_and_footer():
    items, meta = parse_email(SYNTHETIC_ALERT)
    urls = {canonical_url(i.url) for i in items}
    assert urls == {"https://www.reddit.com/r/SaaS/comments/abc123/need_better_invoicing",
                    "https://news.ycombinator.com/item?id=777"}
    reddit = next(i for i in items if "reddit" in i.url)
    assert reddit.title == "Need better invoicing" and "recurring invoices" in reddit.snippet
    assert reddit.community == "r/SaaS" and reddit.query == "wish there was a tool" and reddit.source == "f5bot"
    assert meta["subject"].startswith("[F5Bot]")


def test_service_email_yields_no_items():
    welcome = b"From: F5Bot <admin@f5bot.com>\nSubject: Welcome to F5Bot\nContent-Type: text/html\n\n<a href='https://f5bot.com/dash'>Add</a>"
    assert parse_email(welcome)[0] == []
