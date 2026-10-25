"""F5Bot alert emails, read over IMAP from a dedicated Gmail account (or label)."""
from __future__ import annotations

import imaplib
import re
from datetime import timedelta

from .. import config
from ..models import FetchResult, utcnow
from .base import Source
from .f5bot_parser import parse_email


SERVICE_SUBJECT = re.compile(r"welcome|verify|confirm|password|your account|receipt|invoice|billing|subscription", re.I)


def _quote(folder: str) -> str:
    return f'"{folder}"' if " " in folder and not folder.startswith('"') else folder


class F5BotEmail(Source):
    name = "f5bot_email"

    def configured(self):
        if not (config.env("IMAP_USER") and config.env("IMAP_PASSWORD")):
            return False, "IMAP_USER / IMAP_PASSWORD not set in .env (see SETUP.md step 3)"
        return True, ""

    def connect(self):
        host = config.env("IMAP_HOST", "imap.gmail.com")
        conn = imaplib.IMAP4_SSL(host, timeout=30)
        conn.login(config.env("IMAP_USER"), config.env("IMAP_PASSWORD"))
        return conn

    def fetch(self) -> FetchResult:
        res = FetchResult()
        folder = self.cfg.get("folder", "INBOX")
        sender = self.cfg.get("sender", "f5bot")
        cap = int(self.cfg.get("max_emails_per_run", 300))
        factory = getattr(self.ctx, "imap_factory", None) or self.connect
        try:
            conn = factory()
        except Exception as exc:  # noqa: BLE001 - surfaced in the dashboard
            res.errors.append(f"IMAP login failed: {exc}. Check IMAP_USER / IMAP_PASSWORD (app password).")
            return res
        try:
            typ, _ = conn.select(_quote(folder), readonly=True)
            if typ != "OK":
                res.errors.append(f"Mailbox/label '{folder}' not found. Create the Gmail label and filter (SETUP.md step 3).")
                return res
            validity = self._uidvalidity(conn)
            key = f"imap:{folder}:{validity}"
            last_uid = int(self.ctx.db.kv_get(f"cursor:{key}", "0") or 0)
            uids = self._search(conn, sender, last_uid)
            if len(uids) > cap:
                res.note = f"{len(uids) - cap} older emails left for the next run"
                uids = uids[:cap]
            for uid in uids:
                typ, data = conn.uid("FETCH", str(uid), "(RFC822)")
                raw = next((p[1] for p in data if isinstance(p, tuple)), None) if typ == "OK" else None
                if raw is None:
                    res.errors.append(f"could not fetch message uid {uid}")
                    break  # stop here so the cursor does not skip it
                rec = {"folder": folder, "uidvalidity": validity, "uid": uid,
                       "raw": raw.decode("utf-8", "replace")}
                try:
                    items, meta = parse_email(raw)
                    service = not items and bool(SERVICE_SUBJECT.search(meta["subject"]))
                    rec.update(subject=meta["subject"], message_id=meta["message_id"], received_at=meta["received_at"],
                               parse_status="ok" if items else ("skipped" if service else "failed"),
                               item_count=len(items),
                               error="" if items else ("F5Bot service message, not an alert" if service
                                                       else "no Reddit/HN/Lobsters links found in this email"))
                    res.items += items
                except Exception as exc:  # noqa: BLE001 - keep the raw email, flag it
                    rec.update(parse_status="failed", error=f"parser error: {exc}")
                res.raw_emails.append(rec)
                res.cursors[f"cursor:{key}"] = str(uid)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return res

    @staticmethod
    def _uidvalidity(conn) -> str:
        typ, data = conn.response("UIDVALIDITY")
        return data[0].decode() if data and data[0] else "0"

    def _search(self, conn, sender: str, last_uid: int) -> list[int]:
        crit = ["FROM", f'"{sender}"']
        if last_uid:
            crit = ["UID", f"{last_uid + 1}:*"] + crit
        else:
            since = utcnow() - timedelta(days=config.get(self.ctx.cfg, "app.first_run_lookback_days", 7))
            crit += ["SINCE", since.strftime("%d-%b-%Y")]
        typ, data = conn.uid("SEARCH", None, *crit)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {data}")
        return sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last_uid)
