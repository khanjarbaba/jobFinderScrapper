"""Claude-scored matching backend - catches nuance a keyword scorer can't
(a "Product" title whose real responsibilities are marketing, an implied-
but-not-explicit sponsorship signal, a seniority range spelled out in prose
rather than a clean regex-able number). Costs a small amount of API usage
per new posting; see matching/heuristic.py for the free alternative.

Employer-type weighting is applied deterministically in code (not left to
the model's own discretion) so the service-company penalty is guaranteed
and configurable via config.yaml -> employer_type.service_based_penalty,
per the requirement that it be "noticeable," not a minor bump. Location
tier is also computed deterministically (matching.common.classify_location_tier)
rather than asked of the model - it's a string-matching problem, not a
judgment call.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from anthropic import Anthropic

from ingest.base import JobPosting
from matching.common import MatchResult, classify_location_tier, keyword_hard_filter
from settings import env

logger = logging.getLogger("matching.llm")

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = env("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
        _client = Anthropic(api_key=api_key)
    return _client


PROMPT_TEMPLATE = """You are screening a job posting for a candidate targeting a \
TECHNICAL product role (Product Owner / Product Manager / Technical Product \
Manager / Technical Product Owner), 5-8 years of experience, background in \
ATS/HRTech integrations (B2B SaaS, PaaS/API platforms, OAuth, integration \
products).

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
  "employer_type": "<product|service> - product if this is a B2B SaaS/\
product-based/PaaS/API-platform company owning its own product, service if \
this is an IT services/consulting/staff-augmentation shop building client \
projects>,
  "sponsorship_confidence": "<confirmed|likely|unconfirmed|null> - ONLY \
meaningful if the listing is international (not remote, not in India): \
confirmed if the JD explicitly states visa sponsorship/H1B/relocation \
assistance, likely if it strongly implies it without an exact phrase, \
unconfirmed otherwise, null if the listing is remote or India-based>,
  "sponsorship_evidence": "<if international, a short quote or paraphrase \
of the JD line supporting sponsorship_confidence; null otherwise or if \
there is no such line>"
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
    keyword_reason = keyword_hard_filter(job, cfg)
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

    location_tier = classify_location_tier(job, cfg)
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
