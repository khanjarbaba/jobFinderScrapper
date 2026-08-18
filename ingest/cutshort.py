"""Cutshort — NOT a viable ingestion source, kept as a documented dead end.

The original spec assumed Cutshort exposed a documented REST API usable for
job discovery ("check cutshort.io/a/devdocs for current auth"). Verified
live against https://developers.cutshort.io/introduction during this build:
Cutshort's public API (developers.cutshort.io) is scoped entirely to
EMPLOYERS — search candidates, unlock candidate contact details, post a job,
invite candidates to apply, manage pipeline stages. There is no endpoint for
a job seeker (or an agent acting on one's behalf) to browse/search job
postings. There's also a Cutshort MCP server, but it's the same
employer-facing surface (candidate search for hiring), not a jobs feed.

So this module is a permanent no-op, not a "not implemented yet" stub like
the Tier 2/3 scraper placeholders. If Cutshort job listings are wanted, the
only path is a rate-limited HTML scraper against their public jobs pages
(e.g. cutshort.io/jobs/...-in-<city>), same pattern as naukri.py/instahyre.py
— that would belong in ingest/cutshort_scraper.py, not here, if ever built.
"""
from __future__ import annotations

from ingest.base import JobPosting, stub_source


def fetch(app_config: dict) -> list[JobPosting]:
    return stub_source(
        "cutshort",
        "Cutshort's public API is recruiter/candidate-search only, no job-listing endpoint exists",
    )
