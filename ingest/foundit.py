"""Foundit (formerly Monster India) — no official public API.

STUB: not yet implemented, see ingest/naukri.py for the shared rationale.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source("foundit", "no official API; scraper not yet implemented")
