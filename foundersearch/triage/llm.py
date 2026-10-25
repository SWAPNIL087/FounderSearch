"""Stage 2: LLM judgement through any OpenAI-compatible chat completions endpoint (OpenAI, Azure OpenAI,
Gemini's OpenAI-compatible endpoint, Groq, Ollama...)."""
from __future__ import annotations

import json
import re
import time

import httpx

from .. import config
from .rules import Verdict

SYSTEM = """You are the triage filter for a founder's research dashboard. You receive a JSON list of posts, \
comments and questions collected from public sites. Decide for each whether it is a useful signal for the owner.

OWNER PROFILE AND GOALS
{profile}

CATEGORIES
- problem: a specific, real pain, complaint or unmet need. Strongest when it is about a product/service the person \
already pays for (price, missing feature, bad support, switching away) or about repetitive work they would pay to avoid.
- client: someone asking for a developer, freelancer, co-founder, tool or automation, ideally with intent to hire or pay.
- junk: promotion, spam, news, memes, vague opinions, hypothetical chatter, job ads for employees, or a keyword hit \
that carries no real need.

SCORING (0-100)
85-100 specific, current, clearly actionable; 60-84 real need but some detail missing; 40-59 plausible, weak or vague; \
0-39 not useful. Use category "junk" for anything below 40.

RULES
- Judge only the text provided. Never invent details. If the text is too short or ambiguous, score at most 45 and say so.
- "reason" is one plain sentence (max 25 words) that quotes or paraphrases the evidence, so a human can verify it.
- Respond with JSON only: {{"results":[{{"i":<index>,"category":"problem|client|junk","score":<int>,"reason":"..."}}]}}
- Return exactly one result per input item, using the same "i".
{examples}"""


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, api_version: str = "", transport=None,
                 timeout: float = 90, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.api_version = api_version
        self.transport = transport
        self.timeout = timeout
        self.sleep = sleep

    @classmethod
    def from_env(cls, transport=None) -> "LLMClient | None":
        key = config.env("LLM_API_KEY")
        if not key:
            return None
        return cls(config.env("LLM_BASE_URL", "https://api.openai.com/v1"), key,
                   config.env("LLM_MODEL", "gpt-4o-mini"), config.env("LLM_API_VERSION", "2024-06-01"), transport)

    @property
    def is_azure(self) -> bool:
        return ".azure.com" in self.base_url

    def complete_json(self, system: str, user: str) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        params = {}
        if self.is_azure:
            headers["api-key"] = self.api_key
            params["api-version"] = self.api_version
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}}
        last = ""
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            for attempt in range(3):
                try:
                    r = client.post(url, json=body, headers=headers, params=params)
                except httpx.HTTPError as exc:
                    last = f"network error: {exc}"
                    self.sleep(2 ** attempt)
                    continue
                if r.status_code == 400 and "response_format" in body:
                    body.pop("response_format")  # endpoint does not support JSON mode; rely on the prompt
                    continue
                if r.status_code == 200:
                    try:
                        return _loads(r.json()["choices"][0]["message"]["content"])
                    except (KeyError, IndexError, ValueError) as exc:
                        raise LLMError(f"unreadable model response: {exc}") from exc
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
                self.sleep(2 ** (attempt + 1))
        raise LLMError(last)


def _loads(content: str) -> dict:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return json.loads(content)


def build_system(cfg: dict, good: list[dict], bad: list[dict]) -> str:
    ex = ""
    if good or bad:
        ex = "\nEXAMPLES FROM THE OWNER'S OWN DECISIONS (learn from these)\n"
        for g in good:
            ex += f"- KEPT ({g.get('category') or 'signal'}): {g['title'][:120]} | {g['snippet'][:160]}\n"
        for b in bad:
            ex += f"- REJECTED: {b['title'][:120]} | {b['snippet'][:160]}\n"
    return SYSTEM.format(profile=config.get(cfg, "triage.profile", "").strip(), examples=ex)


def classify_batch(llm: LLMClient, cfg: dict, items: list[dict], good: list[dict], bad: list[dict]) -> dict[int, Verdict]:
    """Returns verdicts keyed by index into `items`. Items the model skipped or garbled are simply absent."""
    max_chars = int(config.get(cfg, "triage.max_chars", 700))
    payload = [{"i": i, "source": it["source"], "community": it.get("community", ""),
                "matched_search": it.get("hint", ""), "title": (it.get("title") or "")[:200],
                "text": (it.get("snippet") or "")[:max_chars]} for i, it in enumerate(items)]
    data = llm.complete_json(build_system(cfg, good, bad), json.dumps(payload, ensure_ascii=False))
    out: dict[int, Verdict] = {}
    for r in data.get("results", []):
        try:
            i, cat = int(r["i"]), str(r["category"]).lower()
            score = max(0, min(100, int(r["score"])))
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= i < len(items) or cat not in ("problem", "client", "junk"):
            continue
        out[i] = Verdict(cat, score, str(r.get("reason", ""))[:300], "llm")
    return out
