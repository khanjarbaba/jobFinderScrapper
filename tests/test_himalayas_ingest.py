"""Regression test for a real bug found against live data: Himalayas
listings were unconditionally tagged remote_type="remote" regardless of
the API's locationRestrictions field, which is an eligibility restriction
(who can take the job), not a description of an open-to-anyone remote role.
This caused e.g. a "Product Manager, restricted to Malaysia" listing to be
grouped under the digest's Remote tier as if an India-based candidate could
actually take it.
"""
from __future__ import annotations

from ingest.himalayas import _to_job_posting


def _raw_job(**overrides) -> dict:
    base = {
        "title": "Technical Product Manager",
        "companyName": "Acme",
        "description": "A B2B SaaS platform role.",
        "applicationLink": "https://himalayas.app/jobs/acme-tpm",
        "employmentType": "Full-time",
    }
    base.update(overrides)
    return base


def test_no_restrictions_is_open_remote():
    job = _to_job_posting(_raw_job(locationRestrictions=[]))
    assert job.remote_type == "remote"
    assert job.location == "Worldwide (remote)"


def test_worldwide_restriction_is_open_remote():
    job = _to_job_posting(_raw_job(locationRestrictions=["Worldwide"]))
    assert job.remote_type == "remote"


def test_india_restriction_is_open_remote():
    job = _to_job_posting(_raw_job(locationRestrictions=["India"]))
    assert job.remote_type == "remote"


def test_other_country_restriction_is_not_open_remote():
    job = _to_job_posting(_raw_job(locationRestrictions=["Malaysia"]))
    assert job.remote_type != "remote"
    assert job.location == "Malaysia"
