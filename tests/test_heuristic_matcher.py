"""Tests for the free, zero-API matching backend (matching/heuristic.py).

No monkeypatching needed here - unlike matching/llm.py, this backend makes
no network calls at all, so these exercise the real regex/keyword logic
directly, including known weak spots documented in heuristic.py's docstring
(marketing-flavor detection needs real density, not one mention).
"""
from __future__ import annotations

import copy

import pytest

from ingest.base import JobPosting
from matching import heuristic
from settings import load_config


@pytest.fixture
def cfg():
    config = copy.deepcopy(load_config())
    config["matching"]["mode"] = "heuristic"
    return config


def test_strong_product_company_match_crosses_threshold(cfg):
    job = JobPosting(
        title="Technical Product Manager",
        company="ApiFlow Inc",
        location="Worldwide (remote)",
        description=(
            "We are a B2B SaaS API platform building integrations for enterprise "
            "customers, using OAuth throughout. Own the roadmap and backlog, write "
            "PRDs, work closely with engineering on the API integration surface. "
            "5-7 years of technical product management experience required."
        ),
        url="https://example.com/1",
        source="test",
        remote_type="remote",
        employment_type="full_time",
    )
    result = heuristic.score_posting(job, cfg)

    assert not result.hard_filtered
    assert result.location_tier == 1
    assert result.employer_type == "product"
    assert result.role_fit_score >= cfg["matching"]["score_threshold"]


def test_service_company_gets_penalized_below_threshold(cfg):
    job = JobPosting(
        title="Product Manager",
        company="ServiceCo IT Consulting",
        location="Bangalore, India",
        description=(
            "IT services and staff augmentation company delivering client projects "
            "for consulting engagements. 6 years experience managing client "
            "professional services delivery as Product Manager."
        ),
        url="https://example.com/2",
        source="test",
        remote_type="onsite",
        employment_type="full_time",
    )
    result = heuristic.score_posting(job, cfg)

    assert not result.hard_filtered
    assert result.location_tier == 2
    assert result.employer_type == "service"
    assert result.raw_role_fit_score > result.role_fit_score  # penalty actually applied
    assert result.role_fit_score < result.raw_role_fit_score


def test_marketing_flavored_product_title_is_hard_filtered(cfg):
    job = JobPosting(
        title="Product Manager - Growth Marketing",
        company="BrandCo",
        location="Remote",
        description=(
            "Own campaign strategy and brand positioning. Run performance marketing "
            "and paid media across channels, manage the content calendar, drive SEO "
            "and social media growth, own ad spend and conversion campaigns."
        ),
        url="https://example.com/3",
        source="test",
        remote_type="remote",
        employment_type="full_time",
    )
    result = heuristic.score_posting(job, cfg)

    assert result.hard_filtered
    assert "marketing" in result.hard_filter_reason.lower()


def test_internship_hard_filtered_before_any_scoring(cfg):
    job = JobPosting(
        title="Product Management Intern",
        company="StartupX",
        location="Remote",
        description="3-month internship, stipend provided.",
        url="https://example.com/4",
        source="test",
        remote_type="remote",
        employment_type="internship",
    )
    result = heuristic.score_posting(job, cfg)

    assert result.hard_filtered
    assert result.role_fit_score is None


@pytest.mark.parametrize(
    "years_text,expected_hard_filtered",
    [
        ("2-3 years of experience required", True),   # junior
        ("12+ years of experience required", True),   # senior
        ("5-7 years of experience required", False),  # fits
        ("great communication skills needed", False),  # unclear -> not excluded
    ],
)
def test_seniority_signal_gates_correctly(cfg, years_text, expected_hard_filtered):
    job = JobPosting(
        title="Product Manager",
        company="Acme SaaS",
        location="Worldwide (remote)",
        description=f"B2B SaaS product management role. {years_text}.",
        url="https://example.com/5",
        source="test",
        remote_type="remote",
        employment_type="full_time",
    )
    result = heuristic.score_posting(job, cfg)
    assert result.hard_filtered == expected_hard_filtered


def test_tier3_requires_exact_sponsorship_phrase(cfg):
    sponsored = JobPosting(
        title="Technical Product Owner",
        company="EuroTech GmbH",
        location="Berlin, Germany",
        description=(
            "We sponsor work visas and provide relocation assistance. B2B SaaS API "
            "platform, 5-6 years experience with integrations."
        ),
        url="https://example.com/6",
        source="test",
        remote_type="onsite",
        employment_type="full_time",
    )
    unsponsored = JobPosting(
        title="Technical Product Owner",
        company="EuroTech GmbH",
        location="Berlin, Germany",
        description="B2B SaaS API platform, 5-6 years experience with integrations.",
        url="https://example.com/7",
        source="test",
        remote_type="onsite",
        employment_type="full_time",
    )

    sponsored_result = heuristic.score_posting(sponsored, cfg)
    unsponsored_result = heuristic.score_posting(unsponsored, cfg)

    assert sponsored_result.location_tier == 3
    assert not sponsored_result.hard_filtered
    assert sponsored_result.sponsorship_confidence == "confirmed"

    assert unsponsored_result.location_tier == 3
    assert unsponsored_result.hard_filtered
    assert "sponsorship_confidence" in unsponsored_result.hard_filter_reason
