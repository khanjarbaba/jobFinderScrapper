"""Loads config.yaml + .env into one place every module can import from."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


@lru_cache(maxsize=1)
def load_config(path: str | Path = ROOT_DIR / "config.yaml") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def data_path(relative: str) -> Path:
    p = ROOT_DIR / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
