"""Builds the single reviewable Markdown digest for a run.

No auto-apply, no CV generation here by design - this only formats matched,
salary-enriched postings into copy-paste-ready blocks grouped by location
tier (Remote / Domestic / International), sorted by role-fit score
(already employer-type-weighted by matching/matcher.py) descending within
each tier.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIER_HEADERS = {
    1: "## \U0001F3E0 Remote",
    2: "## \U0001F4CD Domestic (Gurugram/Bangalore/Hyderabad/Mumbai)",
    3: "## ✈️ International (Europe/USA — sponsorship confirmed or likely)",
}

EMPLOYER_TYPE_LABEL = {
    "product": "Product/PaaS-SaaS",
    "service": "Service-based",
}


def _employer_label(employer_type: str | None) -> str:
    return EMPLOYER_TYPE_LABEL.get(employer_type or "", "Unknown")


def _format_posting_block(posting: dict[str, Any]) -> str:
    company = posting["company"] or "Unknown company"
    title = posting["title"] or "Unknown title"
    location = posting["location"] or "Not specified"
    employer_label = _employer_label(posting.get("employer_type"))
    score = posting.get("role_fit_score")
    reason = posting.get("match_reason") or ""
    salary_display = posting.get("salary_display") or "Not disclosed"
    url = posting["url"]
    description = posting["description"] or "(no description text available)"

    lines = [
        "---",
        f"## {company} — {title}",
        f"Location: {location} | Employer type: {employer_label}",
        f"Match score: {score}/100 — {reason}",
        f"Salary: {salary_display}",
    ]

    if posting.get("location_tier") == 3:
        confidence = posting.get("sponsorship_confidence") or "unconfirmed"
        evidence = posting.get("sponsorship_evidence")
        evidence_part = f" — {evidence}" if evidence else ""
        lines.append(f"Sponsorship signal: {confidence}{evidence_part}")

    lines.append(f"Apply link: {url}")
    lines.append("")
    lines.append("### JD (paste this into CV Project)")
    lines.append("```")
    lines.append(description)
    lines.append("```")
    lines.append("---")
    return "\n".join(lines)


def build_digest(postings: list[dict[str, Any]], fx_label: str, run_started_at: datetime | None = None) -> str:
    run_started_at = run_started_at or datetime.now(timezone.utc)
    by_tier: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: []}
    for posting in postings:
        tier = posting.get("location_tier")
        if tier in by_tier:
            by_tier[tier].append(posting)

    for tier_postings in by_tier.values():
        tier_postings.sort(key=lambda p: p.get("role_fit_score") or 0, reverse=True)

    total = sum(len(v) for v in by_tier.values())
    header = [
        f"# Job Digest — {run_started_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"{total} posting(s) matched above threshold. All compensation figures are "
        f"in INR; foreign-currency conversions use {fx_label}.",
        "",
    ]

    sections: list[str] = []
    for tier in (1, 2, 3):
        tier_postings = by_tier[tier]
        sections.append(TIER_HEADERS[tier])
        sections.append("")
        if not tier_postings:
            sections.append("_No matches this run._")
            sections.append("")
            continue
        for posting in tier_postings:
            sections.append(_format_posting_block(posting))
            sections.append("")

    return "\n".join(header + sections)


def write_digest(postings: list[dict[str, Any]], fx_label: str, digest_dir: str | Path) -> Path:
    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    content = build_digest(postings, fx_label, run_started_at=now)
    out_path = digest_dir / f"{now.strftime('%Y-%m-%d')}.md"
    out_path.write_text(content, encoding="utf-8")

    latest_path = digest_dir / "latest.md"
    latest_path.write_text(content, encoding="utf-8")
    return out_path
