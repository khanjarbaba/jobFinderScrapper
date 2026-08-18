"""JSearch (RapidAPI) — aggregates LinkedIn/Indeed/Glassdoor/etc. listings
through a licensed API, so this is NOT direct scraping of any of those sites.

Requires RAPIDAPI_KEY (see .env.example) and sources.tier1.jsearch.enabled: true
in config.yaml. Disabled by default since it costs API quota. Field names
below match JSearch's documented response shape as of this build; if RapidAPI
changes their schema, this module degrades gracefully (missing fields -> None)
rather than crashing the run.
"""
from __future__ import annotations

import logging

from ingest.base import JobPosting, RateLimiter, http_get
from settings import env

logger = logging.getLogger("ingest.jsearch")

API_HOST = "jsearch.p.rapidapi.com"
SEARCH_URL = f"https://{API_HOST}/search"
_rate_limiter = RateLimiter(min_interval_seconds=1.0)


def _to_job_posting(job: dict) -> JobPosting:
    city = job.get("job_city") or ""
    country = job.get("job_country") or ""
    location = ", ".join(p for p in [city, country] if p) or "Not specified"

    salary_text = None
    if job.get("job_min_salary") or job.get("job_max_salary"):
        currency = job.get("job_salary_currency", "")
        period = job.get("job_salary_period", "")
        salary_text = (
            f"{currency} {job.get('job_min_salary', '')}-{job.get('job_max_salary', '')} "
            f"{period}"
        ).strip()

    remote_type = "remote" if job.get("job_is_remote") else None

    return JobPosting(
        title=job.get("job_title", ""),
        company=job.get("employer_name", ""),
        location=location,
        description=job.get("job_description", ""),
        url=job.get("job_apply_link", ""),
        source="jsearch",
        posted_date=job.get("job_posted_at_datetime_utc"),
        visa_sponsorship=None,
        remote_type=remote_type,
        employment_type=job.get("job_employment_type"),
        salary_text=salary_text,
        raw=job,
    )


def fetch(app_config: dict) -> list[JobPosting]:
    api_key = env("RAPIDAPI_KEY")
    if not api_key:
        logger.info("RAPIDAPI_KEY not set, skipping jsearch")
        return []

    keywords: list[str] = app_config["search"]["keywords"]
    per_source_cap: int = app_config["search"]["results_per_source_per_run"]
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": API_HOST}

    results: dict[str, JobPosting] = {}
    for keyword in keywords:
        _rate_limiter.wait()
        resp = http_get(
            SEARCH_URL,
            params={"query": f"{keyword} in India", "num_pages": 1},
            headers=headers,
        )
        payload = resp.json()
        for job in payload.get("data", []):
            posting = _to_job_posting(job)
            if posting.url:
                results[posting.url] = posting
        if len(results) >= per_source_cap:
            break

    return list(results.values())[:per_source_cap]
