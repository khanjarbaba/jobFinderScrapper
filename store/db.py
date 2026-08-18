"""SQLite persistence: seen postings, match scores, salary data, status.

Status lifecycle (spec): new -> matched -> cv_generated -> queued_for_review
-> applied -> rejected/skipped. This pipeline only ever sets new, skipped
(hard-filtered or below threshold), matched, and queued_for_review — the
cv_generated/applied/rejected states are set manually once a human acts on
the digest (see set_status(), exposed for that purpose).
"""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ingest.base import JobPosting

VALID_STATUSES = {
    "new", "matched", "cv_generated", "queued_for_review", "applied",
    "rejected", "skipped",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    description TEXT,
    posted_date TEXT,
    visa_sponsorship INTEGER,      -- 0/1, NULL = unknown
    remote_type TEXT,
    employment_type TEXT,
    salary_text TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    url_hash TEXT PRIMARY KEY REFERENCES postings(url_hash),
    role_fit_score INTEGER,
    match_reason TEXT,
    location_tier INTEGER,           -- 1 remote, 2 domestic, 3 international
    employer_type TEXT,              -- 'product' | 'service'
    sponsorship_confidence TEXT,     -- 'confirmed' | 'likely' | 'unconfirmed' | NULL
    sponsorship_evidence TEXT,       -- short quote/paraphrase from the JD, tier 3 only
    hard_filtered INTEGER NOT NULL DEFAULT 0,
    hard_filter_reason TEXT,
    matched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS salaries (
    url_hash TEXT PRIMARY KEY REFERENCES postings(url_hash),
    salary_display TEXT NOT NULL,    -- ready-to-print string, e.g. "Est. ₹18-24 LPA"
    salary_min_inr REAL,
    salary_max_inr REAL,
    is_estimated INTEGER NOT NULL DEFAULT 0,
    source_note TEXT,
    enriched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS salary_lookup_cache (
    cache_key TEXT PRIMARY KEY,      -- company|role_title_norm|location, lowercased
    salary_display TEXT NOT NULL,
    salary_min_inr REAL,
    salary_max_inr REAL,
    looked_up_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(status);
CREATE INDEX IF NOT EXISTS idx_matches_tier_score
    ON matches(location_tier, role_fit_score);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- postings -----------------------------------------------------

    def upsert_posting(self, job: JobPosting) -> tuple[str, bool]:
        """Insert if new, else touch last_seen_at. Returns (url_hash, is_new)."""
        url_hash = job.url_hash()
        now = _now()
        existing = self.conn.execute(
            "SELECT url_hash FROM postings WHERE url_hash = ?", (url_hash,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE postings SET last_seen_at = ? WHERE url_hash = ?",
                (now, url_hash),
            )
            self.conn.commit()
            return url_hash, False

        self.conn.execute(
            """
            INSERT INTO postings (
                url_hash, url, source, title, company, location, description,
                posted_date, visa_sponsorship, remote_type, employment_type,
                salary_text, status, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                url_hash, job.url, job.source, job.title, job.company,
                job.location, job.description, job.posted_date,
                None if job.visa_sponsorship is None else int(job.visa_sponsorship),
                job.remote_type, job.employment_type, job.salary_text,
                now, now,
            ),
        )
        self.conn.commit()
        return url_hash, True

    def get_posting(self, url_hash: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM postings WHERE url_hash = ?", (url_hash,)
        ).fetchone()

    def set_status(self, url_hash: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}, must be one of {VALID_STATUSES}")
        self.conn.execute(
            "UPDATE postings SET status = ? WHERE url_hash = ?", (status, url_hash)
        )
        self.conn.commit()

    def postings_needing_match(self) -> list[sqlite3.Row]:
        """New postings that have not been scored yet (survives interrupted runs)."""
        return self.conn.execute(
            """
            SELECT p.* FROM postings p
            LEFT JOIN matches m ON m.url_hash = p.url_hash
            WHERE m.url_hash IS NULL
            """
        ).fetchall()

    # ---- matches --------------------------------------------------------

    def save_match(
        self,
        url_hash: str,
        *,
        role_fit_score: Optional[int],
        match_reason: str,
        location_tier: Optional[int],
        employer_type: Optional[str],
        sponsorship_confidence: Optional[str] = None,
        sponsorship_evidence: Optional[str] = None,
        hard_filtered: bool = False,
        hard_filter_reason: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO matches (
                url_hash, role_fit_score, match_reason, location_tier,
                employer_type, sponsorship_confidence, sponsorship_evidence,
                hard_filtered, hard_filter_reason, matched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                role_fit_score=excluded.role_fit_score,
                match_reason=excluded.match_reason,
                location_tier=excluded.location_tier,
                employer_type=excluded.employer_type,
                sponsorship_confidence=excluded.sponsorship_confidence,
                sponsorship_evidence=excluded.sponsorship_evidence,
                hard_filtered=excluded.hard_filtered,
                hard_filter_reason=excluded.hard_filter_reason,
                matched_at=excluded.matched_at
            """,
            (
                url_hash, role_fit_score, match_reason, location_tier,
                employer_type, sponsorship_confidence, sponsorship_evidence,
                int(hard_filtered), hard_filter_reason, _now(),
            ),
        )
        new_status = "skipped" if hard_filtered else "matched"
        self.conn.execute(
            "UPDATE postings SET status = ? WHERE url_hash = ? AND status = 'new'",
            (new_status, url_hash),
        )
        self.conn.commit()

    # ---- salaries ---------------------------------------------------------

    def save_salary(
        self,
        url_hash: str,
        *,
        salary_display: str,
        salary_min_inr: Optional[float] = None,
        salary_max_inr: Optional[float] = None,
        is_estimated: bool = False,
        source_note: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO salaries (
                url_hash, salary_display, salary_min_inr, salary_max_inr,
                is_estimated, source_note, enriched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                salary_display=excluded.salary_display,
                salary_min_inr=excluded.salary_min_inr,
                salary_max_inr=excluded.salary_max_inr,
                is_estimated=excluded.is_estimated,
                source_note=excluded.source_note,
                enriched_at=excluded.enriched_at
            """,
            (
                url_hash, salary_display, salary_min_inr, salary_max_inr,
                int(is_estimated), source_note, _now(),
            ),
        )
        self.conn.commit()

    def get_salary_lookup_cache(self, cache_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM salary_lookup_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()

    def set_salary_lookup_cache(
        self, cache_key: str, salary_display: str,
        salary_min_inr: Optional[float], salary_max_inr: Optional[float],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO salary_lookup_cache (
                cache_key, salary_display, salary_min_inr, salary_max_inr, looked_up_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                salary_display=excluded.salary_display,
                salary_min_inr=excluded.salary_min_inr,
                salary_max_inr=excluded.salary_max_inr,
                looked_up_at=excluded.looked_up_at
            """,
            (cache_key, salary_display, salary_min_inr, salary_max_inr, _now()),
        )
        self.conn.commit()

    # ---- digest query ---------------------------------------------------

    def postings_for_digest(self, score_threshold: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT p.*, m.role_fit_score, m.match_reason, m.location_tier,
                   m.employer_type, m.sponsorship_confidence, m.sponsorship_evidence,
                   s.salary_display
            FROM postings p
            JOIN matches m ON m.url_hash = p.url_hash
            LEFT JOIN salaries s ON s.url_hash = p.url_hash
            WHERE m.hard_filtered = 0
              AND m.role_fit_score >= ?
              AND p.status IN ('matched', 'queued_for_review')
            """,
            (score_threshold,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_queued_for_review(self, url_hashes: Iterable[str]) -> None:
        self.conn.executemany(
            "UPDATE postings SET status = 'queued_for_review' WHERE url_hash = ?",
            [(h,) for h in url_hashes],
        )
        self.conn.commit()


@contextlib.contextmanager
def open_store(db_path: str | Path):
    store = JobStore(db_path)
    try:
        yield store
    finally:
        store.close()
