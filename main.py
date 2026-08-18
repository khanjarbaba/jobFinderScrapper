"""Orchestrates ingest -> dedup -> match -> enrich salary -> build digest.

Idempotent: re-running never duplicates a posting (dedup is by URL hash),
never re-spends a match/salary call on something already scored/enriched
in a prior run, and always regenerates digests/latest.md from current DB
state. Never auto-applies, never generates a CV - the digest is the only
output a human is meant to act on.
"""
from __future__ import annotations

import logging
import sys

from ingest import (
    adzuna, arbeitnow, cutshort, foundit, himalayas, hirist, iimjobs,
    instahyre, jsearch, naukri, remoteok, usponsorme, weworkremotely, wellfound,
)
from ingest.base import JobPosting, safe_fetch
from matching import matcher
from review_queue import digest as digest_module
from salary_enrichment import enrich
from settings import env, load_config, data_path
from store.db import open_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")

SOURCE_REGISTRY = {
    "tier1": {
        "himalayas": himalayas,
        "remoteok": remoteok,
        "weworkremotely": weworkremotely,
        "jsearch": jsearch,
        "adzuna": adzuna,
    },
    "tier2": {
        "cutshort": cutshort,
        "naukri": naukri,
        "instahyre": instahyre,
        "hirist": hirist,
        "foundit": foundit,
        "iimjobs": iimjobs,
    },
    "tier3": {
        "arbeitnow": arbeitnow,
        "wellfound": wellfound,
        "usponsorme": usponsorme,
    },
}


def _row_to_job_posting(row) -> JobPosting:
    return JobPosting(
        title=row["title"],
        company=row["company"],
        location=row["location"],
        description=row["description"] or "",
        url=row["url"],
        source=row["source"],
        posted_date=row["posted_date"],
        visa_sponsorship=None if row["visa_sponsorship"] is None else bool(row["visa_sponsorship"]),
        remote_type=row["remote_type"],
        employment_type=row["employment_type"],
        salary_text=row["salary_text"],
    )


def run_ingest(cfg: dict) -> list[JobPosting]:
    all_postings: list[JobPosting] = []
    for tier_name, sources in SOURCE_REGISTRY.items():
        tier_cfg = cfg["sources"].get(tier_name, {})
        for source_name, module in sources.items():
            source_cfg = tier_cfg.get(source_name, {})
            if not source_cfg.get("enabled"):
                logger.info("[%s] disabled in config.yaml, skipping", source_name)
                continue
            postings = safe_fetch(source_name, module.fetch, cfg)
            all_postings.extend(postings)
    logger.info("ingest complete: %d postings fetched across all enabled sources", len(all_postings))
    return all_postings


def run_dedup(cfg: dict, postings: list[JobPosting], store) -> tuple[int, int]:
    new_count = 0
    seen_count = 0
    for job in postings:
        if not job.url or not job.title:
            continue
        _, is_new = store.upsert_posting(job)
        if is_new:
            new_count += 1
        else:
            seen_count += 1
    logger.info("dedup complete: %d new postings, %d already seen", new_count, seen_count)
    return new_count, seen_count


def run_matching(cfg: dict, store) -> int:
    mode = cfg["matching"].get("mode", "llm")
    if mode == "llm" and not env("ANTHROPIC_API_KEY"):
        logger.warning(
            "matching.mode is 'llm' but ANTHROPIC_API_KEY is not set - skipping "
            "matching + salary enrichment for this run. Postings were still "
            "ingested and deduped; they'll be scored on the next run once the "
            "key is available. Set matching.mode: heuristic in config.yaml for "
            "free, no-API scoring instead."
        )
        return 0

    rows = store.postings_needing_match()
    logger.info("matching %d postings needing a score", len(rows))
    matched_count = 0
    consecutive_failures = 0
    max_consecutive_failures = 3
    for i, row in enumerate(rows):
        job = _row_to_job_posting(row)
        try:
            result = matcher.score_posting(job, cfg)
        except Exception as exc:  # noqa: BLE001
            # Transient failure (billing, rate limit, network, bad response) -
            # do NOT save a match row, so this posting stays eligible for the
            # next run instead of getting stuck as permanently unscored.
            logger.warning("matching call failed for %r (%s), will retry next run: %s",
                            job.title, job.url, exc)
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                remaining = len(rows) - i - 1
                logger.error(
                    "%d consecutive matching failures, stopping early (likely a "
                    "systemic issue - billing, bad API key, outage). %d untried "
                    "postings will be retried next run.",
                    consecutive_failures, remaining,
                )
                break
            continue

        consecutive_failures = 0
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
        if not result.hard_filtered:
            matched_count += 1
    logger.info("matching complete: %d/%d postings passed hard filters", matched_count, len(rows))
    return matched_count


def run_salary_enrichment(cfg: dict, store) -> str:
    threshold = cfg["matching"]["score_threshold"]
    postings = store.postings_for_digest(threshold)
    logger.info("enriching salary for %d postings above threshold", len(postings))
    for posting in postings:
        if posting.get("salary_display"):
            continue  # already enriched in a prior run
        job = _row_to_job_posting_from_dict(posting)
        result = enrich.enrich_salary(job, cfg, store)
        store.save_salary(
            posting["url_hash"],
            salary_display=result.salary_display,
            salary_min_inr=result.salary_min_inr,
            salary_max_inr=result.salary_max_inr,
            is_estimated=result.is_estimated,
            source_note=result.source_note,
        )
    _, fx_label = enrich.get_fx_rates(cfg)
    return fx_label


def _row_to_job_posting_from_dict(row: dict) -> JobPosting:
    return JobPosting(
        title=row["title"],
        company=row["company"],
        location=row["location"],
        description=row.get("description") or "",
        url=row["url"],
        source=row["source"],
        salary_text=row.get("salary_text"),
        remote_type=row.get("remote_type"),
    )


def run_digest(cfg: dict, store, fx_label: str) -> None:
    threshold = cfg["matching"]["score_threshold"]
    postings = store.postings_for_digest(threshold)
    store.mark_queued_for_review([p["url_hash"] for p in postings])
    out_path = digest_module.write_digest(postings, fx_label, data_path(cfg["output"]["digest_dir"]))
    logger.info("digest written: %s (%d postings)", out_path, len(postings))


def main() -> int:
    cfg = load_config()
    db_path = data_path(cfg["output"]["db_path"])

    with open_store(db_path) as store:
        postings = run_ingest(cfg)
        run_dedup(cfg, postings, store)
        run_matching(cfg, store)
        fx_label = run_salary_enrichment(cfg, store)
        run_digest(cfg, store, fx_label)

    logger.info("run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
