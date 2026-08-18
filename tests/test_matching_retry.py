"""Regression test for a real production bug: a Claude API failure (billing,
rate limit, network) must never be persisted as a permanent hard-filtered
match, or the affected postings would never be retried on a later run.

This is exactly what happened on the first live run with a real API key -
every call failed with "credit balance too low," and the old code saved
that as hard_filtered=True for every posting it touched, permanently
excluding them from re-matching once the account had credits again.
"""
from __future__ import annotations

import main as main_module
from ingest.base import JobPosting
from matching import matcher
from settings import load_config
from store.db import JobStore


def test_run_matching_does_not_persist_transient_failures(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-for-test")
    cfg = load_config()
    store = JobStore(tmp_path / "retry_test.db")

    jobs = [
        JobPosting(
            title=f"Product Manager {i}",
            company="Acme",
            location="Remote",
            description="B2B SaaS product manager role, 5 years experience.",
            url=f"https://example.com/retry/{i}",
            source="test",
            remote_type="remote",
            employment_type="full_time",
        )
        for i in range(5)
    ]
    for job in jobs:
        store.upsert_posting(job)

    call_count = 0

    def always_fail(_job, _cfg):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Your credit balance is too low to access the Anthropic API.")

    monkeypatch.setattr(matcher, "score_posting", always_fail)

    matched = main_module.run_matching(cfg, store)

    assert matched == 0
    # circuit breaker: stop after 3 consecutive failures, don't burn through all 5
    assert call_count == 3

    # nothing was persisted - every posting is still eligible for matching next run
    rows = store.postings_needing_match()
    assert len(rows) == 5

    store.close()
