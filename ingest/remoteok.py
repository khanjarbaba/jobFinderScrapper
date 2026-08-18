"""RemoteOK public JSON feed — no auth, but requires a browser-like User-Agent
or Cloudflare returns 403 (handled by ingest.base.USER_AGENT).

GET https://remoteok.com/api -> JSON array; element [0] is a legal/metadata
notice (has a "legal" key), not a job. Remaining elements are job objects.
There's no free-text search param that reliably matches multi-word titles
like "Technical Product Manager", so we pull the full feed and filter
client-side against config.search.keywords.
"""
from __future__ import annotations

import logging

from ingest.base import DiskCache, JobPosting, RateLimiter, http_get, matches_any_keyword, strip_html

logger = logging.getLogger("ingest.remoteok")

API_URL = "https://remoteok.com/api"
_rate_limiter = RateLimiter(min_interval_seconds=2.0)
_cache = DiskCache("remoteok", ttl_seconds=6 * 3600)


def _to_job_posting(job: dict) -> JobPosting:
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    salary_text = None
    if salary_min or salary_max:
        salary_text = f"${salary_min or ''}-{salary_max or ''}"

    job_id = job.get("id") or job.get("slug", "")
    url = job.get("url") or (f"https://remoteok.com/remote-jobs/{job_id}" if job_id else "")

    return JobPosting(
        title=(job.get("position") or "").strip(),
        company=(job.get("company") or "").strip(),
        location=job.get("location") or "Worldwide (remote)",
        description=strip_html(job.get("description", "")),
        url=url,
        source="remoteok",
        posted_date=job.get("date"),
        visa_sponsorship=None,
        remote_type="remote",
        employment_type=None,
        salary_text=salary_text,
        raw=job,
    )


def fetch(app_config: dict) -> list[JobPosting]:
    keywords: list[str] = app_config["search"]["keywords"]
    per_source_cap: int = app_config["search"]["results_per_source_per_run"]

    cached = _cache.get("full_feed")
    if cached is not None:
        feed = cached
    else:
        _rate_limiter.wait()
        resp = http_get(API_URL)
        feed = resp.json()
        _cache.set("full_feed", feed)

    postings: list[JobPosting] = []
    for job in feed:
        if "legal" in job or not job.get("position"):
            continue
        title = job.get("position", "")
        tags = " ".join(job.get("tags") or [])
        haystack = f"{title} {tags}"
        if not matches_any_keyword(haystack, keywords):
            continue
        postings.append(_to_job_posting(job))
        if len(postings) >= per_source_cap:
            break

    return postings
