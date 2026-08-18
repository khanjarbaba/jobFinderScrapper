"""Naukri.com — India's largest job portal, no official public API.

STUB: not yet implemented. Per the build plan, Tier 1 (API-backed) sources
are validated end-to-end first; Naukri and the other India-portal scrapers
are added one at a time after that, each isolated so a broken selector here
never blocks the others (main.py wraps every source in safe_fetch()).

Implementation plan when this is picked up:
  - Prefer a managed scraping service (e.g. Apify's Naukri actor) over a
    hand-rolled scraper if budget allows — Naukri's markup changes often
    and is behind bot-detection that a managed service already handles.
  - If hand-rolled: rate-limit aggressively (this module would be the most
    likely to get temp-blocked), cache search-result pages in
    ingest.base.DiskCache with a multi-hour TTL, and respect
    https://www.naukri.com/robots.txt.
  - Search by city (config.location.domestic_cities) + keyword
    (config.search.keywords), parse listing cards for title/company/
    location/posted_date/url, then fetch each JD page individually with
    ingest.base.extract_main_text for the full description.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source("naukri", "no official API; scraper not yet implemented")
