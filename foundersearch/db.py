"""SQLite storage. One `items` table; tabs are just values of `status`."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import RawItem, iso
from .normalize import canonical_url, community_from_url, item_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    query TEXT DEFAULT '',
    hint TEXT DEFAULT '',
    title TEXT DEFAULT '',
    url TEXT NOT NULL,
    snippet TEXT DEFAULT '',
    author TEXT DEFAULT '',
    community TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    fetched_at TEXT NOT NULL,
    category TEXT DEFAULT '',
    score INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    method TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    flag TEXT DEFAULT '',
    note TEXT DEFAULT '',
    status_changed_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched_at);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,              -- ok | partial | failed
    fetched INTEGER DEFAULT 0,
    new_items INTEGER DEFAULT 0,
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON source_runs(source, id);

CREATE TABLE IF NOT EXISTS raw_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    uidvalidity TEXT NOT NULL,
    uid INTEGER NOT NULL,
    message_id TEXT DEFAULT '',
    received_at TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    raw TEXT NOT NULL,
    parse_status TEXT DEFAULT 'pending',   -- ok | failed
    item_count INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    UNIQUE(folder, uidvalidity, uid)
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

STATUSES = ("pending", "inbox", "selected", "junk", "deleted")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def tx(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- key/value (cursors, research "last checked") -------------------
    def kv_get(self, key: str, default: str = "") -> str:
        with self.tx() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # ---- merge: insert-only ---------------------------------------------
    def insert_items(self, items: list[RawItem]) -> int:
        """Insert items that do not exist yet. Existing rows are never touched. Returns number added."""
        added = 0
        now = iso()
        with self.tx() as c:
            for it in items:
                if not it.url:
                    continue
                url = canonical_url(it.url)
                cur = c.execute(
                    """INSERT OR IGNORE INTO items
                       (id,source,query,hint,title,url,snippet,author,community,published_at,fetched_at,status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                    (item_id(url), it.source, it.query, it.hint, it.title.strip(), url, it.snippet.strip(),
                     it.author, it.community or community_from_url(url), it.published_at or now, now),
                )
                added += cur.rowcount
        return added

    def add_manual(self, url: str, title: str = "", snippet: str = "", hint: str = "") -> tuple[str, bool]:
        """Deliberate capture by the user: always lands in Inbox, even if it was junked or deleted before."""
        url = canonical_url(url)
        iid = item_id(url)
        now = iso()
        with self.tx() as c:
            row = c.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO items (id,source,hint,title,url,snippet,community,published_at,fetched_at,
                       category,score,reason,method,status,status_changed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (iid, "manual", hint, title.strip(), url, snippet.strip(), community_from_url(url), now, now,
                     hint if hint in ("problem", "client") else "", 100, "Added manually", "manual", "inbox", now),
                )
                return iid, True
            if row["status"] in ("junk", "deleted", "pending"):
                c.execute("UPDATE items SET status='inbox', status_changed_at=?, method='manual' WHERE id=?",
                          (now, iid))
            return iid, False

    # ---- tracker actions -------------------------------------------------
    def set_status(self, item_id_: str, status: str, clear_flag: bool = False) -> bool:
        assert status in STATUSES
        with self.tx() as c:
            sql = "UPDATE items SET status=?, status_changed_at=?" + (", flag=''" if clear_flag else "") + " WHERE id=?"
            return c.execute(sql, (status, iso(), item_id_)).rowcount > 0

    def get_item(self, item_id_: str) -> sqlite3.Row | None:
        with self.tx() as c:
            return c.execute("SELECT * FROM items WHERE id=?", (item_id_,)).fetchone()

    def toggle_flag(self, item_id_: str) -> str | None:
        row = self.get_item(item_id_)
        if row is None:
            return None
        new = "" if row["flag"] == "yellow" else "yellow"
        with self.tx() as c:
            c.execute("UPDATE items SET flag=? WHERE id=?", (new, item_id_))
        return new

    def set_note(self, item_id_: str, note: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE items SET note=? WHERE id=?", (note[:2000], item_id_)).rowcount > 0

    def list_items(self, status: str, search: str = "", source: str = "", category: str = "",
                   limit: int = 2000) -> list[dict]:
        sql = "SELECT * FROM items WHERE status=?"
        args: list = [status]
        if search:
            sql += " AND (title LIKE ? OR snippet LIKE ? OR note LIKE ? OR community LIKE ?)"
            args += [f"%{search}%"] * 4
        if source:
            sql += " AND source=?"
            args.append(source)
        if category:
            sql += " AND category=?"
            args.append(category)
        sql += " ORDER BY published_at DESC, fetched_at DESC LIMIT ?"
        args.append(limit)
        with self.tx() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def counts(self) -> dict[str, int]:
        with self.tx() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
        out = {s: 0 for s in STATUSES}
        out.update({r["status"]: r["n"] for r in rows})
        return out

    def sources_in_use(self) -> list[str]:
        with self.tx() as c:
            return [r["source"] for r in c.execute("SELECT DISTINCT source FROM items ORDER BY source")]

    # ---- triage ------------------------------------------------------------
    def pending(self, limit: int = 200) -> list[dict]:
        with self.tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM items WHERE status='pending' ORDER BY fetched_at LIMIT ?", (limit,))]

    def apply_triage(self, item_id_: str, category: str, score: int, reason: str, method: str, status: str) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE items SET category=?, score=?, reason=?, method=?, status=?, status_changed_at=?
                   WHERE id=? AND status='pending'""",
                (category, score, reason[:300], method, status, iso(), item_id_),
            )

    def bump_attempts(self, ids: list[str]) -> None:
        with self.tx() as c:
            c.executemany("UPDATE items SET attempts=attempts+1 WHERE id=?", [(i,) for i in ids])

    def requeue(self, days: int) -> int:
        """Send inbox/junk items of the last N days back to triage (after editing the profile)."""
        from datetime import timedelta
        from .models import utcnow
        since = iso(utcnow() - timedelta(days=days))
        with self.tx() as c:
            return c.execute(
                """UPDATE items SET status='pending', attempts=0 WHERE status IN ('inbox','junk')
                   AND method!='manual' AND fetched_at>=?""", (since,)).rowcount

    def feedback_examples(self, n: int = 6) -> tuple[list[dict], list[dict]]:
        """Items I kept (selected) and rejected (deleted) - shown to the model as examples."""
        with self.tx() as c:
            good = [dict(r) for r in c.execute(
                "SELECT title,snippet,category FROM items WHERE status='selected' ORDER BY status_changed_at DESC LIMIT ?", (n,))]
            bad = [dict(r) for r in c.execute(
                "SELECT title,snippet FROM items WHERE status='deleted' ORDER BY status_changed_at DESC LIMIT ?", (n,))]
        return good, bad

    # ---- runs / emails -------------------------------------------------------
    def start_run(self, source: str) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO source_runs(source,started_at) VALUES(?,?)", (source, iso())).lastrowid

    def finish_run(self, run_id: int, status: str, fetched: int, new: int, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE source_runs SET finished_at=?, status=?, fetched=?, new_items=?, error=? WHERE id=?",
                      (iso(), status, fetched, new, error[:1500], run_id))

    def last_runs(self) -> dict[str, dict]:
        """Latest run and latest successful run per source."""
        out: dict[str, dict] = {}
        with self.tx() as c:
            for r in c.execute("""SELECT * FROM source_runs WHERE id IN
                                  (SELECT MAX(id) FROM source_runs WHERE finished_at IS NOT NULL GROUP BY source)"""):
                out[r["source"]] = {"last": dict(r), "last_ok": None}
            for r in c.execute("""SELECT * FROM source_runs WHERE id IN
                                  (SELECT MAX(id) FROM source_runs WHERE status IN ('ok','partial') GROUP BY source)"""):
                out.setdefault(r["source"], {"last": None})["last_ok"] = dict(r)
        return out

    def store_email(self, rec: dict) -> int | None:
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO raw_emails(folder,uidvalidity,uid,message_id,received_at,subject,raw,
                   parse_status,item_count,error) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rec["folder"], rec["uidvalidity"], rec["uid"], rec.get("message_id", ""), rec.get("received_at", ""),
                 rec.get("subject", ""), rec["raw"], rec.get("parse_status", "pending"), rec.get("item_count", 0),
                 rec.get("error", "")))
            return cur.lastrowid if cur.rowcount else None

    def failed_emails(self) -> list[dict]:
        with self.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM raw_emails WHERE parse_status='failed' ORDER BY id")]

    def update_email(self, email_id: int, status: str, count: int, error: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE raw_emails SET parse_status=?, item_count=?, error=? WHERE id=?",
                      (status, count, error[:500], email_id))

    def email_stats(self) -> dict:
        with self.tx() as c:
            r = c.execute("SELECT COUNT(*) total, SUM(parse_status='failed') failed, "
                          "SUM(parse_status='ok') parsed FROM raw_emails").fetchone()
        return {"total": r["total"] or 0, "failed": r["failed"] or 0, "parsed": r["parsed"] or 0}

    # ---- backup --------------------------------------------------------------
    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = self._connect()
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
