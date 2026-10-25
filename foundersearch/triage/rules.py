"""Stage 1 (free, instant): rules that need no model, and the keyword scorer used as fallback."""
from __future__ import annotations

from dataclasses import dataclass

from .. import config


@dataclass
class Verdict:
    category: str   # problem | client | junk
    score: int      # 0-100
    reason: str
    method: str     # blocklist | rules | heuristic | llm


def apply_rules(item: dict, cfg: dict) -> Verdict | None:
    """Return a junk verdict for obvious junk, else None (item goes on to the model)."""
    text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    if len(text.strip()) < int(config.get(cfg, "triage.min_chars", 15)):
        return Verdict("junk", 0, "Too little text to judge", "rules")
    for phrase in config.get(cfg, "triage.blocklist", []):
        if phrase.lower() in text:
            return Verdict("junk", 0, f"Blocklist: '{phrase}'", "blocklist")
    return None


def heuristic_score(item: dict, cfg: dict) -> Verdict:
    """Transparent phrase-based scoring. Used when no LLM key is set or the LLM keeps failing."""
    h = config.get(cfg, "triage.heuristics", {})
    text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    problem = [p for p in h.get("problem", []) if p.lower() in text]
    client = [p for p in h.get("client", []) if p.lower() in text]
    paid = [p for p in h.get("paid", []) if p.lower() in text]
    neg = [p for p in h.get("negative", []) if p.lower() in text]
    score = 15 + 22 * min(len(problem), 2) + 28 * min(len(client), 2) + 10 * min(len(paid), 2)
    if "?" in text:
        score += 5
    if item.get("hint") and (problem or client):
        score += 5
    score -= 30 * len(neg)
    score = max(0, min(100, score))
    category = "junk" if score < 35 else ("client" if len(client) > len(problem) else "problem")
    hits = problem + client + paid
    reason = ("Keyword match: " + ", ".join(hits[:4])) if hits else "No strong signal words"
    if neg:
        reason += f" | negative: {', '.join(neg[:2])}"
    return Verdict(category, score, reason, "heuristic")
