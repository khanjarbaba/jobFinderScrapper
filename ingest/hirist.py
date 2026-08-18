"""Hirist — India tech job portal, no official public API.

STUB: not yet implemented, see ingest/naukri.py for the shared rationale.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source("hirist", "no official API; scraper not yet implemented")
