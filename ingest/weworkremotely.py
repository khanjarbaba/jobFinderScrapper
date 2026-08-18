"""We Work Remotely — official per-category RSS feeds, remote-only board.

We use the "Product" category feed (closest fit for PM/PO roles) plus
"Management and Finance" as a secondary net, since WWR's own categories are
coarse. WWR titles are formatted "Company Name: Job Title" in the RSS.

RSS <description> is sometimes a short teaser rather than the full JD. When
that happens we fall back to one extra fetch of the job's own page and run
it through ingest.base.extract_main_text — this is a lightweight GET against
an official listing page (not LinkedIn, not behind auth), rate-limited like
every other source here.
"""
from __future__ import annotations

import logging

import feedparser

from ingest.base import DiskCache, JobPosting, RateLimiter, extract_main_text, http_get, strip_html

logger = logging.getLogger("ingest.weworkremotely")

FEED_URLS = [
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
    "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
]
SHORT_DESCRIPTION_CHARS = 400
_rate_limiter = RateLimiter(min_interval_seconds=2.0)
_cache = DiskCache("weworkremotely", ttl_seconds=6 * 3600)


def _split_title(raw_title: str) -> tuple[str, str]:
    if ": " in raw_title:
        company, title = raw_title.split(": ", 1)
        return company.strip(), title.strip()
    return "Unknown", raw_title.strip()


def _fetch_full_description(url: str) -> str | None:
    cache_key = f"page:{url}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        _rate_limiter.wait()
        resp = http_get(url)
        text = extract_main_text(resp.text)
        _cache.set(cache_key, text)
        return text
    except Exception as exc:  # noqa: BLE001
        logger.info("could not fetch full WWR description for %s: %s", url, exc)
        return None


def _entry_to_job_posting(entry: dict) -> JobPosting:
    company, title = _split_title(entry.get("title", ""))
    description = strip_html(entry.get("summary") or entry.get("description") or "")
    url = entry.get("link", "")

    if len(description) < SHORT_DESCRIPTION_CHARS and url:
        fuller = _fetch_full_description(url)
        if fuller and len(fuller) > len(description):
            description = fuller

    return JobPosting(
        title=title,
        company=company,
        location="Worldwide (remote)",
        description=description,
        url=url,
        source="weworkremotely",
        posted_date=entry.get("published"),
        visa_sponsorship=None,
        remote_type="remote",
        employment_type=None,
        salary_text=None,
        raw=dict(entry),
    )


def fetch(app_config: dict) -> list[JobPosting]:
    keywords: list[str] = app_config["search"]["keywords"]
    per_source_cap: int = app_config["search"]["results_per_source_per_run"]

    postings: list[JobPosting] = []
    for feed_url in FEED_URLS:
        cache_key = f"feed:{feed_url}"
        cached = _cache.get(cache_key)
        if cached is not None:
            raw_entries = cached
        else:
            _rate_limiter.wait()
            parsed = feedparser.parse(feed_url)
            raw_entries = [dict(e) for e in parsed.entries]
            _cache.set(cache_key, raw_entries)

        for entry in raw_entries:
            title_text = entry.get("title", "")
            if not any(kw.lower() in title_text.lower() for kw in keywords):
                continue
            postings.append(_entry_to_job_posting(entry))
            if len(postings) >= per_source_cap:
                return postings

    return postings
