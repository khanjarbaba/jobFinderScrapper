"""Arbeitnow Job Board API — public, no auth, Europe-focused, exposes a
`visa_sponsorship` boolean per listing. This is our primary Tier 3 (Europe)
source precisely because sponsorship is a first-class field, not something
we have to infer from JD text.

Base URL confirmed live: https://www.arbeitnow.com/api/job-board-api
The public endpoint has no free-text query parameter, so we pull a few
pages and filter client-side against config.search.keywords. If Arbeitnow
changes their response shape, every field access below is defensive
(.get()), so this degrades to fewer/no results rather than raising.
"""
from __future__ import annotations

import logging

from ingest.base import DiskCache, JobPosting, RateLimiter, matches_any_keyword, strip_html, http_get

logger = logging.getLogger("ingest.arbeitnow")

API_URL = "https://www.arbeitnow.com/api/job-board-api"
MAX_PAGES = 3
_rate_limiter = RateLimiter(min_interval_seconds=1.5)
_cache = DiskCache("arbeitnow", ttl_seconds=6 * 3600)


def _to_job_posting(job: dict) -> JobPosting:
    remote = job.get("remote")
    remote_type = "remote" if remote else "onsite"

    job_types = job.get("job_types") or []
    employment_type = job_types[0] if job_types else None

    return JobPosting(
        title=job.get("title", ""),
        company=job.get("company_name", ""),
        location=job.get("location") or "Europe",
        description=strip_html(job.get("description", "")),
        url=job.get("url", ""),
        source="arbeitnow",
        posted_date=str(job.get("created_at")) if job.get("created_at") else None,
        visa_sponsorship=job.get("visa_sponsorship"),
        remote_type=remote_type,
        employment_type=employment_type,
        salary_text=None,
        raw=job,
    )


def fetch(app_config: dict) -> list[JobPosting]:
    keywords: list[str] = app_config["search"]["keywords"]
    per_source_cap: int = app_config["search"]["results_per_source_per_run"]

    postings: list[JobPosting] = []
    for page in range(1, MAX_PAGES + 1):
        cache_key = f"page:{page}"
        cached = _cache.get(cache_key)
        if cached is not None:
            payload = cached
        else:
            _rate_limiter.wait()
            resp = http_get(API_URL, params={"page": page})
            payload = resp.json()
            _cache.set(cache_key, payload)

        jobs = payload.get("data", [])
        if not jobs:
            break

        for job in jobs:
            title = job.get("title", "")
            if not matches_any_keyword(title, keywords):
                continue
            postings.append(_to_job_posting(job))
            if len(postings) >= per_source_cap:
                return postings

        meta = payload.get("meta") or {}
        last_page = meta.get("last_page")
        if last_page and page >= last_page:
            break

    return postings
