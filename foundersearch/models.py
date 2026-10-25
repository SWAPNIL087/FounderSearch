"""Shared data shapes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RawItem:
    """What every source adapter returns. Text fields are stored exactly as received."""
    source: str
    url: str
    title: str = ""
    snippet: str = ""
    author: str = ""
    community: str = ""
    published_at: str = ""
    query: str = ""
    hint: str = ""  # 'problem' | 'client' | '' - which kind of search found it


@dataclass
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cursors: dict[str, str] = field(default_factory=dict)  # written only after items are saved
    raw_emails: list[dict] = field(default_factory=list)
    note: str = ""
