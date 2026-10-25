"""HTTP API + static UI."""
from __future__ import annotations

import html
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import config
from .db import Database
from .engine import Engine
from .models import iso
from .normalize import URL_RE
from .triage.llm import LLMClient

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("foundersearch")


class ManualBody(BaseModel):
    text: str
    hint: str = ""


class NoteBody(BaseModel):
    note: str


class BulkBody(BaseModel):
    ids: list[str]
    action: str  # select | dismiss | restore | untrack


def fetch_title(engine: Engine, url: str) -> str:
    """Best-effort page title for a pasted link. Failure is fine; the URL itself is shown instead."""
    try:
        with engine.ctx.http(timeout=6) as client:
            r = client.get(url)
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text[:50000], re.I | re.S)
        return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    except Exception:  # noqa: BLE001
        return ""


def create_app(cfg: dict | None = None, db: Database | None = None, engine: Engine | None = None,
               start_scheduler: bool = True) -> FastAPI:
    cfg = cfg or config.load_config()
    db = db or Database(config.db_path(cfg))
    engine = engine or Engine(cfg, db, LLMClient.from_env())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_scheduler:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(title="FounderSearch", lifespan=lifespan)
    app.state.engine, app.state.db = engine, db

    def must(ok: bool) -> None:
        if not ok:
            raise HTTPException(404, "item not found")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/items")
    def items(status: str = "inbox", q: str = "", source: str = "", category: str = "",
              limit: int = Query(2000, le=5000)):
        if status not in ("inbox", "selected", "junk"):
            raise HTTPException(400, "status must be inbox, selected or junk")
        return {"items": db.list_items(status, q, source, category, limit)}

    @app.post("/api/items/bulk")
    def bulk(body: BulkBody):
        actions = {"select": ("selected", False), "dismiss": ("deleted", True),
                   "restore": ("inbox", True), "untrack": ("inbox", True)}
        if body.action not in actions:
            raise HTTPException(400, f"action must be one of {sorted(actions)}")
        status_, clear = actions[body.action]
        done = sum(db.set_status(i, status_, clear_flag=clear) for i in body.ids[:500])
        return {"ok": True, "changed": done}

    @app.post("/api/items/{item_id}/select")
    def select(item_id: str):
        must(db.set_status(item_id, "selected"))
        return {"ok": True}

    @app.post("/api/items/{item_id}/delete")
    def delete(item_id: str):
        row = db.get_item(item_id)
        must(row is not None)
        if row["status"] == "selected":  # un-track: goes back to the Inbox
            db.set_status(item_id, "inbox", clear_flag=True)
            return {"ok": True, "moved_to": "inbox"}
        db.set_status(item_id, "deleted", clear_flag=True)
        return {"ok": True, "moved_to": "deleted"}

    @app.post("/api/items/{item_id}/restore")
    def restore(item_id: str):
        must(db.set_status(item_id, "inbox", clear_flag=True))
        return {"ok": True}

    @app.post("/api/items/{item_id}/flag")
    def flag(item_id: str):
        new = db.toggle_flag(item_id)
        must(new is not None)
        return {"flag": new}

    @app.post("/api/items/{item_id}/note")
    def note(item_id: str, body: NoteBody):
        must(db.set_note(item_id, body.note))
        return {"ok": True}

    @app.post("/api/refresh")
    def refresh():
        engine.trigger()
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return {
            "counts": db.counts(),
            "engine": engine.state,
            "sources": engine.health(),
            "emails": db.email_stats(),
            "triage_mode": f"LLM ({engine.llm.model})" if engine.llm else "keyword scorer (no LLM key)",
            "threshold": config.get(cfg, "triage.threshold", 40),
            "all_sources": db.sources_in_use(),
            "now": iso(),
        }

    @app.post("/api/manual")
    def manual(body: ManualBody):
        urls = list(dict.fromkeys(URL_RE.findall(body.text)))[:20]
        if not urls:
            raise HTTPException(400, "No URL found. Paste one or more links (one per line).")
        added = 0
        for u in urls:
            _, new = db.add_manual(u, title=fetch_title(engine, u), hint=body.hint)
            added += new
        return {"links": len(urls), "added": added}

    @app.get("/capture", response_class=HTMLResponse)
    def capture(u: str = "", t: str = "", s: str = "", h: str = ""):
        if not u.startswith("http"):
            return HTMLResponse("<p>No URL received.</p>", status_code=400)
        _, new = db.add_manual(u, title=t[:300], snippet=s[:2000], hint=h)
        msg = "Saved to FounderSearch Inbox" if new else "Already in FounderSearch (moved to Inbox if it was hidden)"
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><body style='font:16px system-ui;padding:24px'>"
            f"<h3>&#10003; {msg}</h3><p style='color:#666'>{html.escape(t[:120])}</p>"
            f"<script>setTimeout(()=>window.close(),1400)</script>")

    @app.get("/api/research")
    def research():
        links = config.get(cfg, "research_links", [])
        return {"links": [{"idx": i, "name": l["name"], "url": l["url"],
                           "last_checked": db.kv_get(f"research:{i}")} for i, l in enumerate(links)]}

    @app.post("/api/research/{idx}/checked")
    def research_checked(idx: int):
        db.kv_set(f"research:{idx}", iso())
        return {"ok": True}

    return app
