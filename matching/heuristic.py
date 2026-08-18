"""Free, zero-API matching backend: regex/keyword scoring only, no Claude
calls at all. Configurable via config.yaml -> matching.mode: "heuristic".

Trade-off, stated plainly: this cannot actually read a JD the way an LLM
can, so it's weakest exactly where the original spec called out that
reading comprehension matters:
  - Marketing-flavored "Product" roles: caught only via keyword density
    (lots of "campaign"/"brand"/"demand gen" language vs. product/technical
    language), which will miss subtler cases an LLM would catch by
    understanding what the role actually does.
  - Sponsorship signal: only exact phrase matches count as "confirmed";
    there's no "likely" tier the way matching/llm.py can infer from
    context, so borderline international listings are more likely to be
    excluded than they would be under matching.mode: "llm".
  - Seniority: only listings that state an explicit year count/range are
    classified; everything else falls through to "unclear" (kept, not
    excluded - see matching/llm.py's docstring on the same default).

Switch to matching.mode: "llm" once ANTHROPIC_API_KEY has usable credits
for higher-precision matching - nothing else about the pipeline changes.
"""
from __future__ import annotations

import re

from ingest.base import JobPosting
from matching.common import MatchResult, classify_location_tier, keyword_hard_filter

# Extra marketing-tell phrases beyond config.role.exclude_if_dominant_theme -
# these read as marketing/growth work even when the theme list above wouldn't
# catch them verbatim.
_MARKETING_SIGNAL_WORDS = [
    "campaign", "seo", "sem", "social media", "media buying", "content calendar",
    "email marketing", "performance marketing", "brand awareness", "influencer",
    "paid media", "ad spend", "conversion campaigns",
]
_PRODUCT_SIGNAL_WORDS = [
    "roadmap", "backlog", "user stories", "sprint", "api", "integration",
    "technical", "engineering", "prd", "stakeholder", "oauth", "saas",
    "wireframe", "user research", "a/b test", "feature",
]
_MARKETING_DENSITY_MIN_HITS = 3

_YEARS_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:\+)?\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*years?", re.IGNORECASE)
_YEARS_PLUS_RE = re.compile(r"(\d{1,2})\s*\+\s*years?", re.IGNORECASE)
_YEARS_MIN_RE = re.compile(r"(?:minimum|min\.?|at least)\s*(\d{1,2})\s*years?", re.IGNORECASE)
_SENIOR_TITLE_RE = re.compile(r"\b(director|vp|vice president|head of|chief)\b", re.IGNORECASE)


def _count_hits(text: str, phrases: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for p in phrases if p.lower() in lowered)


def _extract_years_range(text: str) -> tuple[int, int] | None:
    match = _YEARS_RANGE_RE.search(text)
    if match:
        a, b = int(match.group(1)), int(match.group(2))
        return (min(a, b), max(a, b))
    match = _YEARS_PLUS_RE.search(text)
    if match:
        n = int(match.group(1))
        return (n, 99)
    match = _YEARS_MIN_RE.search(text)
    if match:
        n = int(match.group(1))
        return (n, 99)
    return None


def _seniority_signal(job: JobPosting, cfg: dict) -> str:
    if _SENIOR_TITLE_RE.search(job.title):
        return "senior"

    years_cfg = cfg["seniority"]
    years_range = _extract_years_range(job.description)
    if years_range is None:
        return "unclear"

    lo, hi = years_range
    if hi < years_cfg["hard_floor_years"]:
        return "junior"
    if lo >= years_cfg["hard_ceiling_years"]:
        return "senior"
    return "fits"


def _employer_type(job: JobPosting, cfg: dict) -> str:
    et_cfg = cfg["employer_type"]
    prioritize_hits = _count_hits(job.description, et_cfg["prioritize_keywords"])
    deprioritize_hits = _count_hits(job.description, et_cfg["deprioritize_keywords"])
    if deprioritize_hits > 0 and deprioritize_hits > prioritize_hits:
        return "service"
    return "product"


def _sponsorship_confidence(job: JobPosting, cfg: dict) -> tuple[str, str | None]:
    phrases = cfg["sponsorship"]["confirmed_phrases"]
    lowered = job.description.lower()
    for phrase in phrases:
        if phrase.lower() in lowered:
            return "confirmed", phrase
    return "unconfirmed", None


def score_posting(job: JobPosting, cfg: dict) -> MatchResult:
    keyword_reason = keyword_hard_filter(job, cfg)
    if keyword_reason:
        return MatchResult(hard_filtered=True, hard_filter_reason=keyword_reason)

    role_cfg = cfg["role"]
    title_and_lede = f"{job.title} {job.description[:300]}"
    is_product_role = any(
        t.lower() in title_and_lede.lower() for t in role_cfg["titles"]
    ) or "product manager" in title_and_lede.lower() or "product owner" in title_and_lede.lower()
    if not is_product_role:
        return MatchResult(
            hard_filtered=True,
            hard_filter_reason="heuristic: title/lede doesn't read as a product management/ownership role",
        )

    marketing_hits = _count_hits(job.description, _MARKETING_SIGNAL_WORDS + role_cfg["exclude_if_dominant_theme"])
    product_hits = _count_hits(job.description, _PRODUCT_SIGNAL_WORDS)
    if marketing_hits >= _MARKETING_DENSITY_MIN_HITS and marketing_hits > product_hits:
        return MatchResult(
            hard_filtered=True,
            hard_filter_reason=(
                f"heuristic: marketing-signal density ({marketing_hits}) exceeds "
                f"product-signal density ({product_hits}) despite the title"
            ),
        )

    seniority = _seniority_signal(job, cfg)
    if seniority in ("junior", "senior"):
        return MatchResult(hard_filtered=True, hard_filter_reason=f"heuristic seniority mismatch ({seniority})")

    location_tier = classify_location_tier(job, cfg)
    employer_type = _employer_type(job, cfg)
    sponsorship_confidence: str | None = None
    sponsorship_evidence: str | None = None

    if location_tier == 3:
        sponsorship_confidence, sponsorship_evidence = _sponsorship_confidence(job, cfg)
        min_confidence = cfg["sponsorship"]["min_confidence_to_include"]
        rank = {"unconfirmed": 0, "likely": 1, "confirmed": 2}
        if rank.get(sponsorship_confidence, 0) < rank.get(min_confidence, 1):
            return MatchResult(
                hard_filtered=True,
                hard_filter_reason=(
                    f"heuristic: international listing with sponsorship_confidence="
                    f"{sponsorship_confidence!r}, below required {min_confidence!r}"
                ),
                location_tier=location_tier,
                employer_type=employer_type,
                sponsorship_confidence=sponsorship_confidence,
                sponsorship_evidence=sponsorship_evidence,
            )

    # Additive score, capped at 100 by construction: 30 (confirmed product
    # role) + up to 40 (keyword density against search/prioritize keyword
    # lists) + up to 20 (seniority fit) + 10 (title says "Technical").
    combined_keywords = cfg["search"]["keywords"] + cfg["employer_type"]["prioritize_keywords"]
    keyword_hits = _count_hits(job.description, combined_keywords)
    raw_score = 30
    raw_score += min(keyword_hits, 8) * 5
    raw_score += 20 if seniority == "fits" else 10  # "unclear" gets partial credit
    if "technical" in job.title.lower():
        raw_score += 10
    raw_score = min(raw_score, 100)

    weighted_score = raw_score
    if employer_type == "service":
        weighted_score -= cfg["employer_type"]["service_based_penalty"]
    weighted_score = max(0, min(100, weighted_score))

    reason = (
        f"Heuristic match: {keyword_hits} product/SaaS keyword hit(s), "
        f"seniority={seniority}, employer_type={employer_type}"
    )

    return MatchResult(
        hard_filtered=False,
        hard_filter_reason=None,
        role_fit_score=weighted_score,
        raw_role_fit_score=raw_score,
        match_reason=reason,
        location_tier=location_tier,
        employer_type=employer_type,
        sponsorship_confidence=sponsorship_confidence,
        sponsorship_evidence=sponsorship_evidence,
    )
