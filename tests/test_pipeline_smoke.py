"""End-to-end smoke test with no live network/API calls.

Exercises the real store/matching/salary_enrichment/digest code paths on
fixture JobPostings, with only the two Claude API call sites monkeypatched
(matcher._call_claude, enrich._llm_market_estimate, enrich.get_fx_rates).
This proves the plumbing - schema, joins, hard filters, employer-type
weighting, sponsorship gating, salary parsing/caching, tier grouping - works
without needing a real ANTHROPIC_API_KEY or network access.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ingest.base import JobPosting
from matching import matcher
from review_queue import digest as digest_module
from salary_enrichment import enrich
from settings import load_config
from store.db import JobStore

FIXED_FX = {"INR": 1.0, "USD": 87.0, "EUR": 94.0, "GBP": 110.0}

FAKE_LLM_RESPONSES = {
    "https://example.com/job/1": {
        "role_fit_score": 88,
        "reason": "Strong fit: technical PM at a B2B SaaS API platform company",
        "is_technical_product_role": True,
        "is_marketing_flavored": False,
        "seniority_signal": "fits",
        "location_tier": 1,
        "employer_type": "product",
        "sponsorship_confidence": None,
        "sponsorship_evidence": None,
    },
    "https://example.com/job/2": {
        "role_fit_score": 80,
        "reason": "Relevant PM role but at an IT services/consulting company",
        "is_technical_product_role": True,
        "is_marketing_flavored": False,
        "seniority_signal": "fits",
        "location_tier": 2,
        "employer_type": "service",
        "sponsorship_confidence": None,
        "sponsorship_evidence": None,
    },
    "https://example.com/job/3": {
        "role_fit_score": 85,
        "reason": "Technical PO role with explicit visa sponsorship",
        "is_technical_product_role": True,
        "is_marketing_flavored": False,
        "seniority_signal": "fits",
        "location_tier": 3,
        "employer_type": "product",
        "sponsorship_confidence": "confirmed",
        "sponsorship_evidence": "We sponsor work visas and provide relocation assistance",
    },
    "https://example.com/job/5": {
        "role_fit_score": 40,
        "reason": "Actually a growth marketing role despite the Product title",
        "is_technical_product_role": True,
        "is_marketing_flavored": True,
        "seniority_signal": "fits",
        "location_tier": 1,
        "employer_type": "product",
        "sponsorship_confidence": None,
        "sponsorship_evidence": None,
    },
    "https://example.com/job/6": {
        "role_fit_score": 90,
        "reason": "Strong fit: B2B SaaS integrations PM role",
        "is_technical_product_role": True,
        "is_marketing_flavored": False,
        "seniority_signal": "fits",
        "location_tier": 1,
        "employer_type": "product",
        "sponsorship_confidence": None,
        "sponsorship_evidence": None,
    },
}


def _fixture_jobs() -> list[JobPosting]:
    return [
        JobPosting(
            title="Senior Technical Product Manager",
            company="Acme SaaS",
            location="Worldwide (remote)",
            description="B2B SaaS integration platform. Own the API platform roadmap. 5-7 years experience.",
            url="https://example.com/job/1",
            source="test",
            remote_type="remote",
            employment_type="full_time",
            salary_text="₹18-24 LPA",
        ),
        JobPosting(
            title="Product Manager",
            company="ServiceCo IT Consulting",
            location="Bangalore, India",
            description="IT services company delivering client projects. 6 years experience managing client engagements.",
            url="https://example.com/job/2",
            source="test",
            remote_type="onsite",
            employment_type="full_time",
        ),
        JobPosting(
            title="Technical Product Owner",
            company="EuroTech GmbH",
            location="Berlin, Germany",
            description="We sponsor work visas and provide relocation assistance. 5+ years with API platforms.",
            url="https://example.com/job/3",
            source="arbeitnow",
            remote_type="onsite",
            employment_type="full_time",
            visa_sponsorship=True,
            salary_text="€60000-75000",
        ),
        JobPosting(
            title="Product Management Intern",
            company="StartupX",
            location="Remote",
            description="3-month internship, stipend provided.",
            url="https://example.com/job/4",
            source="test",
            remote_type="remote",
            employment_type="internship",
        ),
        JobPosting(
            title="Product Manager - Growth Marketing",
            company="BrandCo",
            location="Remote",
            description="Own campaign strategy, brand positioning, and demand generation for our product line.",
            url="https://example.com/job/5",
            source="test",
            remote_type="remote",
            employment_type="full_time",
        ),
        JobPosting(
            title="Product Manager - Integrations",
            company="ApiFlow Inc",
            location="Worldwide (remote)",
            description="B2B SaaS API integration platform, OAuth-based integrations, 5-6 years PM experience.",
            url="https://example.com/job/6",
            source="test",
            remote_type="remote",
            employment_type="full_time",
        ),
    ]


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def store(tmp_path: Path):
    s = JobStore(tmp_path / "test.db")
    yield s
    s.close()


def test_end_to_end_pipeline(monkeypatch, cfg, store):
    def fake_call_claude(job, _cfg):
        return FAKE_LLM_RESPONSES[job.url]

    monkeypatch.setattr(matcher, "_call_claude", fake_call_claude)
    monkeypatch.setattr(enrich, "get_fx_rates", lambda _cfg: (FIXED_FX, "fixed test rates"))

    llm_estimate_calls = []

    def fake_llm_estimate(job, _cfg):
        llm_estimate_calls.append(job.url)
        return enrich.SalaryResult(
            salary_display="Est. ₹22-28 LPA",
            salary_min_inr=2_200_000,
            salary_max_inr=2_800_000,
            is_estimated=True,
            source_note="AI-estimated (test)",
        )

    monkeypatch.setattr(enrich, "_llm_market_estimate", fake_llm_estimate)

    jobs = _fixture_jobs()

    # --- ingest/dedup ---
    url_hashes = {}
    for job in jobs:
        url_hash, is_new = store.upsert_posting(job)
        assert is_new, f"expected {job.url} to be new on first insert"
        url_hashes[job.url] = url_hash

    # idempotency: inserting again must not be "new"
    _, is_new_again = store.upsert_posting(jobs[0])
    assert not is_new_again

    # --- matching ---
    rows = store.postings_needing_match()
    assert len(rows) == len(jobs)
    for row in rows:
        job = next(j for j in jobs if j.url == row["url"])
        result = matcher.score_posting(job, cfg)
        store.save_match(
            row["url_hash"],
            role_fit_score=result.role_fit_score,
            match_reason=result.match_reason,
            location_tier=result.location_tier,
            employer_type=result.employer_type,
            sponsorship_confidence=result.sponsorship_confidence,
            sponsorship_evidence=result.sponsorship_evidence,
            hard_filtered=result.hard_filtered,
            hard_filter_reason=result.hard_filter_reason,
        )

    # job/4 (internship) hard-filtered by the cheap keyword pass, never hits the LLM
    job4_hash = url_hashes["https://example.com/job/4"]
    row4 = store.get_posting(job4_hash)
    assert row4["status"] == "skipped"

    # job/2 (service company) survives hard filters but employer-type penalty
    # (raw 80 - 20 = 60) should push it below the default threshold of 70
    threshold = cfg["matching"]["score_threshold"]
    assert threshold == 70

    # --- salary enrichment (only for postings that will make the digest) ---
    digest_postings = store.postings_for_digest(threshold)
    digest_urls = {p["url"] for p in digest_postings}
    assert digest_urls == {
        "https://example.com/job/1",  # remote, product, weighted 88
        "https://example.com/job/3",  # international, sponsorship confirmed, 85
        "https://example.com/job/6",  # remote, product, 90, no stated salary -> LLM estimate
    }
    # job/2 filtered by threshold (weighted score), job/4 by keyword hard filter,
    # job/5 by is_marketing_flavored despite its "Product Manager" title
    assert "https://example.com/job/2" not in digest_urls
    assert "https://example.com/job/5" not in digest_urls

    for posting in digest_postings:
        job = next(j for j in jobs if j.url == posting["url"])
        result = enrich.enrich_salary(job, cfg, store)
        store.save_salary(
            posting["url_hash"],
            salary_display=result.salary_display,
            salary_min_inr=result.salary_min_inr,
            salary_max_inr=result.salary_max_inr,
            is_estimated=result.is_estimated,
            source_note=result.source_note,
        )

    # job/1 and job/3 had stated salary_text -> regex parse, no LLM call needed
    # job/6 had none -> exactly one LLM estimate call
    assert llm_estimate_calls == ["https://example.com/job/6"]

    # re-running enrichment for the same postings must hit the cache, not the LLM again
    job6 = next(j for j in jobs if j.url == "https://example.com/job/6")
    enrich.enrich_salary(job6, cfg, store)
    assert llm_estimate_calls == ["https://example.com/job/6"], "expected cache hit, not a second LLM call"

    # --- digest ---
    digest_postings = store.postings_for_digest(threshold)
    fx_label = "fixed test rates"
    md = digest_module.build_digest(digest_postings, fx_label)

    assert "## \U0001F3E0 Remote" in md
    assert "## \U0001F4CD Domestic" in md
    assert "International" in md
    assert "Acme SaaS" in md
    assert "EuroTech GmbH" in md
    assert "ApiFlow Inc" in md
    assert "ServiceCo IT Consulting" not in md
    assert "BrandCo" not in md
    assert "₹18-24 LPA, stated" in md
    assert "Sponsorship signal: confirmed" in md
    assert "Est. ₹22-28 LPA" in md

    # Remote section should list job/6 (score 90) before job/1 (score 88)
    remote_section = md.split("Domestic")[0]
    assert remote_section.index("ApiFlow Inc") < remote_section.index("Acme SaaS")
