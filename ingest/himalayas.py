"""Himalayas Remote Jobs API — public, no auth, remote-only listings.

Docs verified live: https://himalayas.app/docs/remote-jobs-api
  GET https://himalayas.app/jobs/api/search?q=<term>&page=<n>
  -> {"updatedAt", "offset", "limit", "totalCount", "jobs": [ {...} ]}
Server-side data refreshes every 24h, so we cache locally for 6h to be a
good citizen without missing same-day updates.
"""
from __future__ import annotations

import logging

from ingest.base import DiskCache, JobPosting, RateLimiter, http_get

logger = logging.getLogger("ingest.himalayas")

SEARCH_URL = "https://himalayas.app/jobs/api/search"
_rate_limiter = RateLimiter(min_interval_seconds=1.5)
_cache = DiskCache("himalayas", ttl_seconds=6 * 3600)


def _to_job_posting(job: dict) -> JobPosting:
    min_sal = job.get("minSalary")
    max_sal = job.get("maxSalary")
    currency = job.get("currency") or ""
    period = job.get("salaryPeriod") or ""
    salary_text = None
    if min_sal or max_sal:
        salary_text = f"{currency} {min_sal or ''}-{max_sal or ''} {period}".strip()

    locations = job.get("locationRestrictions") or []
    location = ", ".join(locations) if locations else "Worldwide (remote)"

    return JobPosting(
        title=job.get("title", "").strip(),
        company=job.get("companyName", "").strip(),
        location=location,
        description=job.get("description") or job.get("excerpt") or "",
        url=job.get("applicationLink") or f"https://himalayas.app/jobs/{job.get('guid', '')}",
        source="himalayas",
        posted_date=job.get("pubDate"),
        visa_sponsorship=None,
        remote_type="remote",
        employment_type=job.get("employmentType"),
        salary_text=salary_text,
        raw=job,
    )


def fetch(app_config: dict) -> list[JobPosting]:
    keywords: list[str] = app_config["search"]["keywords"]
    per_source_cap: int = app_config["search"]["results_per_source_per_run"]

    results: dict[str, JobPosting] = {}  # keyed by url to dedup across keywords
    for keyword in keywords:
        cache_key = f"search:{keyword}"
        cached = _cache.get(cache_key)
        if cached is not None:
            payload = cached
        else:
            _rate_limiter.wait()
            resp = http_get(SEARCH_URL, params={"q": keyword, "page": 1})
            payload = resp.json()
            _cache.set(cache_key, payload)

        for job in payload.get("jobs", []):
            posting = _to_job_posting(job)
            if posting.url not in results:
                results[posting.url] = posting
        if len(results) >= per_source_cap:
            break

    return list(results.values())[:per_source_cap]
