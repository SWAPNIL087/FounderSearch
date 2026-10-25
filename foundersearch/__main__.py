"""Command line: python -m foundersearch <command>"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config
from .db import Database
from .engine import Engine, reparse_emails
from .triage.llm import LLMClient


def setup_logging(cfg: dict) -> None:
    log_dir = Path(cfg["_root"]) / "data"
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), RotatingFileHandler(log_dir / "foundersearch.log", maxBytes=1_000_000,
                                                          backupCount=3, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def bookmarklet(port: int) -> str:
    js = (f"javascript:(function(){{window.open('http://127.0.0.1:{port}/capture?u='+encodeURIComponent(location.href)"
          f"+'&t='+encodeURIComponent(document.title)+'&s='+encodeURIComponent(String(getSelection())),"
          f"'foundersearch','width=440,height=220')}})()")
    return js


def cmd_run(cfg, db, args):
    import uvicorn
    from .api import create_app
    host, port = config.get(cfg, "app.host", "127.0.0.1"), int(config.get(cfg, "app.port", 8765))
    print(f"FounderSearch running at http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(cfg, db), host=host, port=port, log_level="warning")


def cmd_fetch(cfg, db, args):
    engine = Engine(cfg, db, LLMClient.from_env())
    print(engine.run_cycle(force=True))
    for s in engine.health():
        print(f"  {s['name']:<14} {s['state']:<15} {s['detail']}")


def cmd_backup(cfg, db, args):
    dest = db.path.parent / "backups" / f"foundersearch-manual-{date.today()}.db"
    db.backup_to(dest)
    print(f"Backup written: {dest}")


def cmd_export(cfg, db, args):
    out = Path(args.path)
    with db.tx() as c, open(out, "w", newline="", encoding="utf-8") as fh:
        rows = c.execute("SELECT * FROM items ORDER BY published_at DESC").fetchall()
        if rows:
            w = csv.writer(fh)
            w.writerow(rows[0].keys())
            w.writerows([tuple(r) for r in rows])
    print(f"Exported {len(rows)} items to {out}")


def cmd_reparse(cfg, db, args):
    fixed, added = reparse_emails(db)
    print(f"Re-parsed failed emails: {fixed} fixed, {added} items added")


def cmd_rescore(cfg, db, args):
    n = db.requeue(args.days)
    print(f"{n} items from the last {args.days} days queued for re-classification (run 'fetch' or wait for the next cycle).")


def cmd_inspect(cfg, db, args):
    from .sources.f5bot_parser import parse_email
    with db.tx() as c:
        rows = c.execute("SELECT * FROM raw_emails ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    if not rows:
        print("No stored emails yet. Run 'python -m foundersearch fetch' after F5Bot has sent an alert.")
    for r in rows:
        items, meta = parse_email(r["raw"].encode("utf-8"))
        print(f"\n=== uid {r['uid']} | {r['subject']} | stored status: {r['parse_status']} | items: {len(items)}")
        for it in items:
            print(f"  - {it.title[:90]!r}\n    {it.url}\n    {it.snippet[:140]!r}")
        if not items:
            print("  (nothing extracted) " + r["error"])


def cmd_bookmarklet(cfg, db, args):
    print(bookmarklet(int(config.get(cfg, "app.port", 8765))))


def cmd_doctor(cfg, db, args):
    from .doctor import run_doctor
    sys.exit(run_doctor(cfg, db))


def main(argv=None):
    p = argparse.ArgumentParser(prog="foundersearch", description="FounderSearch - problem & client signal dashboard")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="start the dashboard and the scheduler (default)")
    sub.add_parser("fetch", help="run one fetch + classify cycle now, without the UI")
    sub.add_parser("doctor", help="check configuration, mailbox, LLM and every source")
    sub.add_parser("backup", help="copy the database to data/backups")
    e = sub.add_parser("export", help="export all items to CSV")
    e.add_argument("path", nargs="?", default="foundersearch-export.csv")
    sub.add_parser("reparse", help="re-run the F5Bot email parser over emails that failed to parse")
    r = sub.add_parser("rescore", help="re-classify recent inbox/junk items (after editing the profile)")
    r.add_argument("--days", type=int, default=7)
    i = sub.add_parser("inspect-email", help="show how stored F5Bot emails are parsed")
    i.add_argument("-n", type=int, default=3)
    sub.add_parser("bookmarklet", help="print the browser bookmarklet for one-click capture")
    args = p.parse_args(argv)

    cfg = config.load_config()
    setup_logging(cfg)
    db = Database(config.db_path(cfg))
    {"run": cmd_run, "fetch": cmd_fetch, "doctor": cmd_doctor, "backup": cmd_backup, "export": cmd_export,
     "reparse": cmd_reparse, "rescore": cmd_rescore, "inspect-email": cmd_inspect,
     "bookmarklet": cmd_bookmarklet}.get(args.cmd or "run", cmd_run)(cfg, db, args)


if __name__ == "__main__":
    main()
