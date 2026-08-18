"""Regression test for a real gap found against live data: a WWR listing
stated "Yearly gross salary range: USD 48,000 - 65,000" in its free-text
description, but job.salary_text was empty (WWR's ingest module never
populates it - only sources with a structured salary API field do), so
enrich_salary() fell straight to "Not disclosed" instead of finding the
number that was right there in the text.
"""
from __future__ import annotations

import copy

import pytest

from ingest.base import JobPosting
from salary_enrichment import enrich
from settings import load_config


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


FIXED_FX = {"INR": 1.0, "USD": 87.0, "EUR": 94.0, "GBP": 110.0}


def test_stated_salary_field_used_when_present(cfg, monkeypatch):
    monkeypatch.setattr(enrich, "get_fx_rates", lambda _cfg: (FIXED_FX, "fixed"))
    job = JobPosting(
        title="Product Manager", company="Acme", location="Remote",
        description="No salary mentioned in the body.",
        url="https://example.com/1", source="test",
        salary_text="₹20-25 LPA",
    )

    class DummyStore:
        def get_salary_lookup_cache(self, _key):
            return None

    result = enrich.enrich_salary(job, cfg, DummyStore())
    assert "stated" in result.salary_display
    assert result.salary_min_inr == 2_000_000


def test_falls_back_to_description_when_salary_text_empty(cfg, monkeypatch):
    monkeypatch.setattr(enrich, "get_fx_rates", lambda _cfg: (FIXED_FX, "fixed"))
    job = JobPosting(
        title="Technical Product Manager", company="MailerLite", location="Worldwide (remote)",
        description=(
            "We are looking for an experienced Technical Product Manager. "
            "Yearly gross salary range: USD 48,000 - 65,000. "
            "Parenting budget of $1000: a special gift for new parents."
        ),
        url="https://example.com/2", source="weworkremotely",
        salary_text=None,
    )

    class DummyStore:
        def get_salary_lookup_cache(self, _key):
            return None

    result = enrich.enrich_salary(job, cfg, DummyStore())
    assert "stated" in result.salary_display
    assert result.salary_min_inr == pytest.approx(48_000 * 87.0)
    assert result.salary_max_inr == pytest.approx(65_000 * 87.0)


def test_not_disclosed_when_nothing_found_and_no_api_key(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(enrich, "get_fx_rates", lambda _cfg: (FIXED_FX, "fixed"))
    job = JobPosting(
        title="Product Manager", company="Acme", location="Remote",
        description="No numbers relevant to compensation anywhere in here.",
        url="https://example.com/3", source="test",
        salary_text=None,
    )

    calls = {}

    class DummyStore:
        def get_salary_lookup_cache(self, _key):
            return None

        def set_salary_lookup_cache(self, key, display, min_inr, max_inr):
            calls["key"] = key
            calls["display"] = display

    result = enrich.enrich_salary(job, cfg, DummyStore())
    assert result.salary_display == "Not disclosed"
    assert calls["display"] == "Not disclosed"
