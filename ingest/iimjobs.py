"""iimjobs — India senior/management-track job portal, no official public API.

STUB: not yet implemented, see ingest/naukri.py for the shared rationale.
Most relevant of the Tier 2 scrapers for the upper end of the 5-8yr band
targeted by config.yaml -> seniority, so worth prioritizing once Tier 2
scraping starts.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source("iimjobs", "no official API; scraper not yet implemented")
