"""Instahyre — India tech job portal, no official public API.

STUB: not yet implemented, see ingest/naukri.py for the shared rationale
(Tier 1 validated first, scrapers added one at a time, each isolated so one
breaking never blocks the others via main.py's safe_fetch() wrapper).

Kept in its own module (rather than folded into a generic "India scraper")
specifically so a markup change or block on Instahyre never takes down
Naukri/Hirist/Foundit/iimjobs in the same run.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source("instahyre", "no official API; scraper not yet implemented")
