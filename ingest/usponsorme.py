"""H1B-sponsor-confirmed board (e.g. USponsorMe-style sources) — Tier 3 only.

STUB: not yet implemented. These sources update weekly rather than daily per
the spec, so once built this should be gated by a separate, longer cache TTL
(e.g. 7 days) rather than the 6h default used elsewhere in ingest/, to avoid
hammering a slow-moving source on every daily cron run.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source("usponsorme", "no confirmed API/feed; scraper not yet implemented")
