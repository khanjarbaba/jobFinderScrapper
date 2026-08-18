"""Wellfound (formerly AngelList Talent) — startup jobs, no official API.

STUB: not yet implemented. Tier 3 (Europe/US, sponsorship-filtered) source —
when implemented, remote_type/location parsing must feed matching/'s
location-tier logic, and visa_sponsorship should stay None (unconfirmed)
unless the JD explicitly states sponsorship, per config.yaml -> sponsorship.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source("wellfound", "no official API; scraper not yet implemented")
