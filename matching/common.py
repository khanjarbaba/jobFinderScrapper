"""Shared types + deterministic logic used by both matching/llm.py and
matching/heuristic.py, so the two scoring backends can never disagree on
the parts that don't actually need judgment (hard filters, location tier).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ingest.base import JobPosting


@dataclass
class MatchResult:
    hard_filtered: bool
    hard_filter_reason: Optional[str]
    role_fit_score: Optional[int] = None       # final, employer-type-weighted
    raw_role_fit_score: Optional[int] = None   # before the employer-type penalty
    match_reason: str = ""
    location_tier: Optional[int] = None
    employer_type: Optional[str] = None
    sponsorship_confidence: Optional[str] = None
    sponsorship_evidence: Optional[str] = None


def _contains_word(haystack: str, phrase: str) -> bool:
    """Word-boundary match, not a plain substring check. A naive `in` check
    on a short keyword like "intern" also matches "international" and
    "internal" - both extremely common in ordinary JD text - which was
    silently hard-filtering ~40% of otherwise-good postings in practice.
    """
    pattern = r"\b" + re.escape(phrase.lower()) + r"\b"
    return re.search(pattern, haystack) is not None


def keyword_hard_filter(job: JobPosting, cfg: dict) -> Optional[str]:
    """Cheap, unambiguous exclusions that never need judgment (LLM or
    otherwise): internship/contract/freelance/unpaid/equity-only.
    """
    haystack = f"{job.title} {job.description}".lower()
    hard_cfg = cfg["hard_filters"]

    if job.employment_type and job.employment_type.lower() in {
        t.lower() for t in hard_cfg["exclude_employment_types"]
    }:
        return f"employment_type={job.employment_type!r} is excluded"

    for kw in hard_cfg["exclude_keywords"]:
        if _contains_word(haystack, kw):
            return f"JD contains excluded keyword {kw!r}"

    return None


def classify_location_tier(job: JobPosting, cfg: dict) -> int:
    """Remote/domestic/international is a string-matching problem, not a
    judgment call - deterministic in both matching backends so the
    sponsorship gate (tier 3 only) always applies consistently regardless
    of which scorer produced the match.

    Unrecognized non-remote locations default to tier 3 (international)
    rather than tier 2 (domestic): treating an unclassified listing as
    domestic would silently skip the sponsorship gate that's supposed to
    protect against unsponsored international listings slipping through.
    """
    loc_cfg = cfg["location"]
    location_lower = (job.location or "").lower()

    if job.remote_type == "remote":
        return 1
    for city in loc_cfg["domestic_cities"]:
        if city.lower() in location_lower:
            return 2
    for region in loc_cfg["international_regions"]:
        if region.lower() in location_lower:
            return 3
    if "india" in location_lower:
        return 2
    return 3
