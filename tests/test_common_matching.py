"""Regression test for a real bug found against live data: the "intern"
hard-filter keyword was matching as a plain substring, so it silently
excluded ~40% of otherwise-good postings just for containing the word
"international" or "internal" anywhere in the JD.
"""
from __future__ import annotations

import copy

import pytest

from ingest.base import JobPosting
from matching.common import keyword_hard_filter
from settings import load_config


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


def _job(description: str, title: str = "Product Manager") -> JobPosting:
    return JobPosting(
        title=title,
        company="Acme",
        location="Remote",
        description=description,
        url="https://example.com/x",
        source="test",
        remote_type="remote",
        employment_type="full_time",
    )


def test_international_does_not_trigger_intern_filter(cfg):
    job = _job("Join our fast-growing international team of product managers.")
    assert keyword_hard_filter(job, cfg) is None


def test_internal_does_not_trigger_intern_filter(cfg):
    job = _job("You'll work closely with internal stakeholders across the org.")
    assert keyword_hard_filter(job, cfg) is None


def test_internet_does_not_trigger_intern_filter(cfg):
    job = _job("We build products used across the internet by millions.")
    assert keyword_hard_filter(job, cfg) is None


def test_actual_internship_is_still_caught(cfg):
    job = _job("This is a 3-month product management internship.")
    reason = keyword_hard_filter(job, cfg)
    assert reason is not None
    assert "internship" in reason.lower()


def test_standalone_intern_mention_is_still_caught(cfg):
    job = _job("We are hiring a product intern to join the team for the summer.")
    reason = keyword_hard_filter(job, cfg)
    assert reason is not None
    assert "intern" in reason.lower()
