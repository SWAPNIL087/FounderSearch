"""Configuration loading: config.yaml (committed) + .env (secrets, never committed)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path or os.getenv("SIGNALBOARD_CONFIG") or ROOT / "config.yaml")
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["_root"] = str(ROOT)
    return cfg


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


def db_path(cfg: dict) -> Path:
    raw = os.getenv("SIGNALBOARD_DB")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p
    return ROOT / "data" / "foundersearch.db"


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()
