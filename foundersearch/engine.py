"""Orchestration: fetch -> merge -> triage, on a timer or on demand. One cycle at a time."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from . import config
from .db import Database
from .models import iso, utcnow
from .sources.base import Context
from .sources.f5bot_email import F5BotEmail
from .sources.f5bot_parser import parse_email
from .sources.github import GitHub
from .sources.hackernews import HackerNews
from .sources.reddit_api import RedditAPI
from .sources.rss import RSS
from .sources.stackexchange import StackExchange
from .triage.llm import LLMClient
from .triage.pipeline import Triager

log = logging.getLogger("foundersearch")

SOURCES = {
    "f5bot_email": F5BotEmail,
    "hackernews": HackerNews,
    "stackexchange": StackExchange,
    "github": GitHub,
    "reddit_api": RedditAPI,
    "rss": RSS,
}


class Engine:
    def __init__(self, cfg: dict, db: Database, llm: LLMClient | None = None, transport=None,
                 imap_factory=None, sleep=time.sleep):
        self.cfg, self.db, self.llm = cfg, db, llm
        self.ctx = Context(cfg=cfg, db=db, transport=transport, sleep=sleep, imap_factory=imap_factory)
        self.triager = Triager(cfg, db, llm)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._force = False
        self._thread: threading.Thread | None = None
        self.state = {"running": False, "last_started": "", "last_finished": "", "last_new": 0, "last_triage": {},
                      "next_run": ""}

    # ---- one cycle ---------------------------------------------------------
    def _is_due(self, name: str, scfg: dict, force: bool) -> bool:
        if force:
            return True
        interval = int(scfg.get("interval_minutes") or config.get(self.cfg, "app.refresh_minutes", 30))
        last = self.db.last_runs().get(name, {}).get("last")
        if not last or last["status"] in ("failed", "skipped", None):
            return True
        finished = datetime.fromisoformat(last["finished_at"])
        return utcnow() - finished >= timedelta(minutes=interval) - timedelta(minutes=1)

    def run_cycle(self, force: bool = False) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"skipped": "a refresh is already running"}
        try:
            self.state.update(running=True, last_started=iso())
            total_new = 0
            for name, cls in SOURCES.items():
                scfg = config.get(self.cfg, f"sources.{name}", {}) or {}
                if not scfg.get("enabled"):
                    continue
                if not self._is_due(name, scfg, force):
                    continue
                total_new += self._run_source(name, cls(self.ctx, scfg))
            tri = self.triager.run()
            self._maybe_backup()
            self.state.update(last_new=total_new, last_triage=tri)
            return {"new": total_new, "triage": tri}
        finally:
            self.state.update(running=False, last_finished=iso())
            self._lock.release()

    def _run_source(self, name: str, src) -> int:
        run_id = self.db.start_run(name)
        ok, why = src.configured()
        if not ok:
            self.db.finish_run(run_id, "skipped", 0, 0, why)
            return 0
        try:
            res = src.fetch()
            for rec in res.raw_emails:
                self.db.store_email(rec)
            new = self.db.insert_items(res.items)
            for key, value in res.cursors.items():  # only after the data is safely stored
                self.db.kv_set(key, value)
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the others
            log.exception("source %s failed", name)
            self.db.finish_run(run_id, "failed", 0, 0, f"{type(exc).__name__}: {exc}")
            return 0
        errors = list(res.errors) + ([res.note] if res.note else [])
        status = "ok"
        if res.errors:
            status = "failed" if not res.items and not res.cursors else "partial"
        self.db.finish_run(run_id, status, len(res.items), new, "; ".join(errors))
        log.info("%s: %s fetched=%d new=%d", name, status, len(res.items), new)
        return new

    def _maybe_backup(self) -> None:
        today = utcnow().strftime("%Y-%m-%d")
        if self.db.kv_get("last_backup") == today:
            return
        try:
            backup_dir = self.db.path.parent / "backups"
            self.db.backup_to(backup_dir / f"foundersearch-{today}.db")
            keep = int(config.get(self.cfg, "app.backup_keep", 14))
            for old in sorted(backup_dir.glob("foundersearch-*.db"))[:-keep]:
                old.unlink(missing_ok=True)
            self.db.kv_set("last_backup", today)
        except Exception:  # noqa: BLE001
            log.exception("backup failed")

    # ---- scheduler thread ----------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="foundersearch-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        self._force = True
        self._wake.set()

    def _loop(self) -> None:
        interval = int(config.get(self.cfg, "app.refresh_minutes", 30)) * 60
        while not self._stop.is_set():
            force, self._force = self._force, False
            try:
                self.run_cycle(force=force)
            except Exception:  # noqa: BLE001
                log.exception("cycle crashed")
            self.state["next_run"] = iso(utcnow() + timedelta(seconds=interval))
            self._wake.wait(interval)
            self._wake.clear()

    # ---- health ----------------------------------------------------------------
    def health(self) -> list[dict]:
        runs = self.db.last_runs()
        out = []
        for name, cls in SOURCES.items():
            scfg = config.get(self.cfg, f"sources.{name}", {}) or {}
            entry = {"name": name, "enabled": bool(scfg.get("enabled")), "state": "disabled", "detail": "",
                     "last_ok": "", "last_run": ""}
            if scfg.get("enabled"):
                r = runs.get(name, {})
                last, last_ok = r.get("last"), r.get("last_ok")
                entry["last_run"] = (last or {}).get("finished_at") or ""
                entry["last_ok"] = (last_ok or {}).get("finished_at") or ""
                interval = int(scfg.get("interval_minutes") or config.get(self.cfg, "app.refresh_minutes", 30))
                if last is None:
                    entry.update(state="never", detail="not run yet")
                elif last["status"] == "skipped":
                    entry.update(state="not_configured", detail=last["error"])
                elif last["status"] == "failed":
                    entry.update(state="failed", detail=last["error"])
                elif last["status"] == "partial":
                    entry.update(state="partial", detail=last["error"])
                else:
                    entry.update(state="ok", detail=f"{last['new_items']} new in last run")
                if entry["state"] in ("ok", "partial") and last_ok:
                    age = utcnow() - datetime.fromisoformat(last_ok["finished_at"])
                    if age > timedelta(minutes=max(interval * 3, 180)):
                        entry.update(state="stale", detail=f"no successful run for {int(age.total_seconds() // 3600)}h")
            out.append(entry)
        return out


def reparse_emails(db: Database) -> tuple[int, int]:
    """Re-run the parser over stored emails that failed (after a parser fix). Returns (emails fixed, items added)."""
    fixed = added = 0
    for rec in db.failed_emails():
        try:
            items, _ = parse_email(rec["raw"].encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            db.update_email(rec["id"], "failed", 0, f"parser error: {exc}")
            continue
        if items:
            added += db.insert_items(items)
            db.update_email(rec["id"], "ok", len(items))
            fixed += 1
    return fixed, added
