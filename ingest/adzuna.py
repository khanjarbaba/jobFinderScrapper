"""Adzuna Jobs API — official, free-tier API (not scraping).

Requires ADZUNA_APP_ID + ADZUNA_APP_KEY (see .env.example) and
sources.tier1.adzuna.enabled: true in config.yaml. Disabled by default.
Queries the India, UK, and US indices so it also feeds Tier 3 candidates
(matching/ decides the tier from location, this module just gathers).
"""
from __future__ import annotations

import logging

from ingest.base import JobPosting, RateLimiter, http_get
from settings import env

logger = logging.getLogger("ingest.adzuna")

COUNTRY_CODES = ["in", "gb", "us"]
_rate_limiter = RateLimiter(min_interval_seconds=1.0)


def _to_job_posting(job: dict, country_code: str) -> JobPosting:
    company = (job.get("company") or {}).get("display_name", "")
    location = (job.get("location") or {}).get("display_name", "")

    salary_text = None
    if job.get("salary_min") or job.get("salary_max"):
        salary_text = f"{job.get('salary_min', '')}-{job.get('salary_max', '')}"

    employment_type = job.get("contract_type") or job.get("contract_time")

    return JobPosting(
        title=job.get("title", ""),
        company=company,
        location=location or country_code.upper(),
        description=job.get("description", ""),
        url=job.get("redirect_url", ""),
        source=f"adzuna_{country_code}",
        posted_date=job.get("created"),
        visa_sponsorship=None,
        remote_type=None,
        employment_type=employment_type,
        salary_text=salary_text,
        raw=job,
    )


def fetch(app_config: dict) -> list[JobPosting]:
    app_id = env("ADZUNA_APP_ID")
    app_key = env("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        logger.info("ADZUNA_APP_ID/ADZUNA_APP_KEY not set, skipping adzuna")
        return []

    keywords: list[str] = app_config["search"]["keywords"]
    per_source_cap: int = app_config["search"]["results_per_source_per_run"]

    results: dict[str, JobPosting] = {}
    for country_code in COUNTRY_CODES:
        for keyword in keywords:
            _rate_limiter.wait()
            url = f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/1"
            resp = http_get(
                url,
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": keyword,
                    "results_per_page": 20,
                    "content-type": "application/json",
                },
            )
            payload = resp.json()
            for job in payload.get("results", []):
                posting = _to_job_posting(job, country_code)
                if posting.url:
                    results[posting.url] = posting
            if len(results) >= per_source_cap:
                break
        if len(results) >= per_source_cap:
            break

    return list(results.values())[:per_source_cap]
