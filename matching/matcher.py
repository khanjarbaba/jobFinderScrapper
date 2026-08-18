"""Scores one JobPosting against the configured role target.

Two-stage filter, cheapest checks first:
  1. Cheap keyword hard filters (no LLM call) - internship/contract/freelance/
     unpaid/equity-only. These are unambiguous from title/employment_type
     text, so there's no reason to spend a Claude call on them.
  2. A single Claude call per surviving posting that reads the JD and
     returns role-fit score, employer type, seniority signal, marketing-vs-
     technical judgment, location tier, and (Tier 3 only) sponsorship
     confidence - the parts that need actual reading comprehension.

Employer-type weighting is applied deterministically in code (not left to
the model's own discretion) so the service-company penalty is guaranteed
and configurable via config.yaml -> employer_type.service_based_penalty,
per the requirement that it be "noticeable," not a minor bump.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from anthropic import Anthropic

from ingest.base import JobPosting
from settings import env

logger = logging.getLogger("matching")

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = env("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
        _client = Anthropic(api_key=api_key)
    return _client


@dataclass
class MatchResult:
    hard_filtered: bool
    hard_filter_reason: Optional[str]
    role_fit_score: Optional[int] = None       # final, employer-type-weighted
    raw_role_fit_score: Optional[int] = None   # as returned by the model
    match_reason: str = ""
    location_tier: Optional[int] = None
    employer_type: Optional[str] = None
    sponsorship_confidence: Optional[str] = None
    sponsorship_evidence: Optional[str] = None


def _keyword_hard_filter(job: JobPosting, cfg: dict) -> Optional[str]:
    haystack = f"{job.title} {job.description}".lower()
    hard_cfg = cfg["hard_filters"]

    if job.employment_type and job.employment_type.lower() in {
        t.lower() for t in hard_cfg["exclude_employment_types"]
    }:
        return f"employment_type={job.employment_type!r} is excluded"

    for kw in hard_cfg["exclude_keywords"]:
        if kw.lower() in haystack:
            return f"JD contains excluded keyword {kw!r}"

    return None


def _location_hint(job: JobPosting, cfg: dict) -> str:
    """Best-effort location hint handed to the model — it still makes the
    final tier call by reading the JD, this just points it at the right facts.
    """
    loc_cfg = cfg["location"]
    location_lower = (job.location or "").lower()
    if job.remote_type == "remote":
        return "Source signal: listing is REMOTE."
    for city in loc_cfg["domestic_cities"]:
        if city.lower() in location_lower:
            return f"Source signal: listing location contains domestic city '{city}'."
    for region in loc_cfg["international_regions"]:
        if region.lower() in location_lower:
            return f"Source signal: listing location contains international region '{region}'."
    return f"Source signal: raw location string = {job.location!r}, remote_type = {job.remote_type!r}."


PROMPT_TEMPLATE = """You are screening a job posting for a candidate targeting a \
TECHNICAL product role (Product Owner / Product Manager / Technical Product \
Manager / Technical Product Owner), 5-8 years of experience, background in \
ATS/HRTech integrations (B2B SaaS, PaaS/API platforms, OAuth, integration \
products). Base location is India; the candidate will consider remote roles \
anywhere, domestic roles in Gurugram/Bangalore/Hyderabad/Mumbai, and \
international roles in Europe/USA only if visa sponsorship or relocation is \
plausible.

{location_hint}

Read the job posting below and respond with ONLY a JSON object (no markdown \
fences, no prose before or after) with exactly these keys:

{{
  "role_fit_score": <int 0-100, how well this JD matches a technical PM/PO \
role for this candidate's background - ignore employer type when scoring \
this, that is judged separately>,
  "reason": "<one sentence, specific to this JD>",
  "is_technical_product_role": <true|false - false if this is not a product \
management/ownership role at all>,
  "is_marketing_flavored": <true|false - true if the ACTUAL responsibilities \
described (campaign ownership, brand strategy, content/demand generation, \
growth marketing) skew marketing even if the title says "Product">,
  "seniority_signal": "<junior|fits|senior|unclear> - junior if JD explicitly \
asks for under 4 years, senior if JD explicitly asks for 10+ years or a \
Director-and-above title, fits if stated range overlaps 5-8 years, unclear \
if no clear signal>,
  "location_tier": <1 if remote, 2 if domestic India (Gurugram/Bangalore/\
Hyderabad/Mumbai) in-office or hybrid, 3 if international (Europe/USA)>,
  "employer_type": "<product|service> - product if this is a B2B SaaS/\
product-based/PaaS/API-platform company owning its own product, service if \
this is an IT services/consulting/staff-augmentation shop building client \
projects>,
  "sponsorship_confidence": "<confirmed|likely|unconfirmed|null> - ONLY \
meaningful for international (tier 3) listings: confirmed if the JD \
explicitly states visa sponsorship/H1B/relocation assistance, likely if it \
strongly implies it without an exact phrase, unconfirmed otherwise, null if \
location_tier is not 3>,
  "sponsorship_evidence": "<if location_tier is 3, a short quote or \
paraphrase of the JD line supporting sponsorship_confidence; null \
otherwise or if there is no such line>"
}}

Job posting:
Title: {title}
Company: {company}
Location: {location}
Source: {source}

Description:
{description}
"""


def _call_claude(job: JobPosting, cfg: dict) -> dict[str, Any]:
    max_chars = cfg["matching"]["max_description_chars"]
    description = job.description[:max_chars]
    prompt = PROMPT_TEMPLATE.format(
        location_hint=_location_hint(job, cfg),
        title=job.title,
        company=job.company,
        location=job.location,
        source=job.source,
        description=description,
    )

    client = _get_client()
    model = cfg["matching"]["model"]
    response = client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    # Defensive: strip markdown fences if the model added them anyway.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in model response: {text[:200]!r}")
    return json.loads(match.group(0))


def score_posting(job: JobPosting, cfg: dict) -> MatchResult:
    keyword_reason = _keyword_hard_filter(job, cfg)
    if keyword_reason:
        return MatchResult(hard_filtered=True, hard_filter_reason=keyword_reason)

    # Deliberately NOT caught here: a Claude call can fail for reasons that
    # have nothing to do with this posting (billing, rate limit, network
    # blip, a malformed response). Swallowing that into a permanent
    # hard_filtered match row would mean this posting - and every other one
    # in the same failed run - never gets retried, since
    # store.postings_needing_match() only checks whether a match row exists
    # at all. The caller (main.py) catches this per-posting and simply
    # skips saving a match, so the posting stays eligible for the next run.
    parsed = _call_claude(job, cfg)

    if not parsed.get("is_technical_product_role", True):
        return MatchResult(
            hard_filtered=True,
            hard_filter_reason="not a product management/ownership role",
            match_reason=parsed.get("reason", ""),
        )
    if parsed.get("is_marketing_flavored"):
        return MatchResult(
            hard_filtered=True,
            hard_filter_reason="responsibilities skew marketing despite title",
            match_reason=parsed.get("reason", ""),
        )
    seniority = parsed.get("seniority_signal")
    if seniority in ("junior", "senior"):
        return MatchResult(
            hard_filtered=True,
            hard_filter_reason=f"seniority mismatch ({seniority})",
            match_reason=parsed.get("reason", ""),
        )

    location_tier = parsed.get("location_tier")
    employer_type = parsed.get("employer_type")
    sponsorship_confidence = parsed.get("sponsorship_confidence")
    sponsorship_evidence = parsed.get("sponsorship_evidence")

    if location_tier == 3:
        min_confidence = cfg["sponsorship"]["min_confidence_to_include"]
        rank = {"unconfirmed": 0, "likely": 1, "confirmed": 2}
        if rank.get(sponsorship_confidence, 0) < rank.get(min_confidence, 1):
            return MatchResult(
                hard_filtered=True,
                hard_filter_reason=(
                    f"international listing with sponsorship_confidence="
                    f"{sponsorship_confidence!r}, below required {min_confidence!r}"
                ),
                match_reason=parsed.get("reason", ""),
                location_tier=location_tier,
                employer_type=employer_type,
                sponsorship_confidence=sponsorship_confidence,
                sponsorship_evidence=sponsorship_evidence,
            )

    raw_score = int(parsed.get("role_fit_score", 0))
    weighted_score = raw_score
    if employer_type == "service":
        weighted_score -= cfg["employer_type"]["service_based_penalty"]
    weighted_score = max(0, min(100, weighted_score))

    return MatchResult(
        hard_filtered=False,
        hard_filter_reason=None,
        role_fit_score=weighted_score,
        raw_role_fit_score=raw_score,
        match_reason=parsed.get("reason", ""),
        location_tier=location_tier,
        employer_type=employer_type,
        sponsorship_confidence=sponsorship_confidence,
        sponsorship_evidence=sponsorship_evidence,
    )
