"""Triage pipeline: rules -> LLM (or keyword scorer) -> route to Inbox / Junk. Failures never drop items."""
from __future__ import annotations

import time

from .. import config
from ..db import Database
from .llm import LLMClient, LLMError, classify_batch
from .rules import Verdict, apply_rules, heuristic_score


def route(v: Verdict, threshold: int) -> str:
    return "inbox" if v.category != "junk" and v.score >= threshold else "junk"


class Triager:
    def __init__(self, cfg: dict, db: Database, llm: LLMClient | None, sleep=time.sleep):
        self.cfg, self.db, self.llm, self.sleep = cfg, db, llm, sleep

    def run(self) -> dict:
        threshold = int(config.get(self.cfg, "triage.threshold", 40))
        batch_size = int(config.get(self.cfg, "triage.batch_size", 10))
        max_attempts = int(config.get(self.cfg, "triage.max_attempts", 5))
        stats = {"inbox": 0, "junk": 0, "pending": 0, "llm_error": ""}

        def save(item: dict, v: Verdict) -> None:
            status = route(v, threshold)
            self.db.apply_triage(item["id"], v.category, v.score, v.reason, v.method, status)
            stats[status] += 1

        to_model: list[dict] = []
        for item in self.db.pending(limit=500):
            v = apply_rules(item, self.cfg)
            if v:
                save(item, v)
            elif self.llm is None or item["attempts"] >= max_attempts:
                save(item, heuristic_score(item, self.cfg))
            else:
                to_model.append(item)

        if to_model:
            good, bad = self.db.feedback_examples()
            pause = float(config.get(self.cfg, "triage.batch_pause_seconds", 0))
            failures = 0
            for start in range(0, len(to_model), batch_size):
                batch = to_model[start:start + batch_size]
                if failures >= 2:  # quota or outage: stop calling, the rest stay pending for the next cycle
                    stats["pending"] += len(batch)
                    continue
                if start and pause:
                    self.sleep(pause)
                try:
                    verdicts = classify_batch(self.llm, self.cfg, batch, good, bad)
                except LLMError as exc:
                    failures += 1
                    stats["llm_error"] = str(exc)
                    self.db.bump_attempts([b["id"] for b in batch])
                    stats["pending"] += len(batch)
                    continue
                failures = 0
                missing = []
                for i, item in enumerate(batch):
                    if i in verdicts:
                        save(item, verdicts[i])
                    else:
                        missing.append(item["id"])
                if missing:
                    self.db.bump_attempts(missing)
                    stats["pending"] += len(missing)
        return stats
