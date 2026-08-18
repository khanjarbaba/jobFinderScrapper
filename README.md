# Job Discovery + CV-Tailoring Pipeline

Finds technical Product Owner / Product Manager roles, scores them against a
configured role target, and writes a single reviewable Markdown digest per
run. It never auto-applies and never generates a CV — CV tailoring stays a
manual step in a separate Claude.ai Project you paste JDs into.

## Pipeline

```
ingest/ -> store/ (dedup) -> matching/ (Claude scoring + hard filters)
        -> salary_enrichment/ -> review_queue/ (digest.md)
```

`main.py` runs all of it and is idempotent — re-running never duplicates a
posting, never re-spends a match or salary call on something already
scored, and always regenerates `digests/latest.md` from current DB state.

## Setup

```bash
git clone https://github.com/khanjarbaba/jobFinderScrapper.git
cd jobFinderScrapper
python -m venv .venv
.venv/Scripts/activate   # or `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:
- `ANTHROPIC_API_KEY` — required. Without it, ingestion still runs and
  postings are stored, but matching/salary enrichment (and therefore the
  digest) are skipped for that run — a warning is logged, nothing crashes.
- `RAPIDAPI_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` — optional, only needed
  if you flip `sources.tier1.jsearch.enabled` / `sources.tier1.adzuna.enabled`
  to `true` in `config.yaml`.

Run it:

```bash
python main.py
```

Output lands in `digests/<date>.md` and `digests/latest.md`. State lives in
`data/jobs.db` (SQLite) — commit it so scheduled runs don't re-score
postings they've already seen.

## Configuration

Everything role/location/seniority/keyword-related is in `config.yaml` —
nothing here is hardcoded in Python. Key sections:

- `role`, `seniority`, `location`, `employer_type` — the matching prompt in
  `matching/matcher.py` is built from these.
- `hard_filters` — cheap keyword checks applied *before* any Claude call
  (internship/contract/freelance/unpaid/equity-only). Marketing-flavored
  "Product" roles and seniority mismatches are also hard-excluded, but that
  needs reading comprehension, so it happens inside the Claude scoring call
  instead (see `matching/matcher.py`'s `is_marketing_flavored` /
  `seniority_signal` fields).
- `matching.score_threshold` — minimum role-fit score (post employer-type
  weighting) to reach the digest. Default 70.
- `sponsorship.min_confidence_to_include` — Tier 3 (international) listings
  below this confidence never reach the digest.
- `sources` — enable/disable individual ingestion sources per tier.

## Sources — current status

**Tier 1 (implemented, API-backed):**
| Source | Auth needed | Notes |
|---|---|---|
| Himalayas | none | public JSON API, remote-only |
| RemoteOK | none | public JSON API, remote-only, filtered client-side by keyword |
| We Work Remotely | none | official RSS (Product + Management/Finance categories); falls back to one extra page fetch when the RSS teaser is short |
| JSearch | `RAPIDAPI_KEY` | aggregates LinkedIn/Indeed/Glassdoor via a licensed API — disabled by default, costs quota |
| Adzuna | `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` | official API, India/UK/US indices — disabled by default |

**Tier 3 (implemented):**
| Source | Notes |
|---|---|
| Arbeitnow | public API, exposes a real `visa_sponsorship` boolean per listing — this is *the* Tier 3 source for that reason |

**Not implemented yet (stubs that return `[]` and log why):**
`naukri`, `instahyre`, `hirist`, `foundit`, `iimjobs` (Tier 2, no official
APIs — need rate-limited scrapers), `wellfound`, `usponsorme` (Tier 3, same
reason). Each is its own module specifically so one breaking/getting
blocked never takes another down — `main.py` wraps every source in
`ingest.base.safe_fetch()`. Add them one at a time; see the docstring in
`ingest/naukri.py` for the intended shape (DiskCache + RateLimiter +
`ingest.base.extract_main_text`).

**Cutshort — permanently excluded, not a stub.** The original plan assumed
Cutshort had a documented job-listing API. It doesn't: `developers.cutshort.io`
is scoped entirely to employers (candidate search, job posting, pipeline
management) — there is no endpoint for a job seeker to browse listings. See
`ingest/cutshort.py` for the full note. If Cutshort listings are wanted
later, it would need an HTML scraper like the Tier 2 sources, not this API.

## Salary enrichment — known limitation

We don't hold API keys for Glassdoor/AmbitionBox/Levels.fyi/Naukri's salary
insights, so the "secondary lookup" fallback (`salary_enrichment/enrich.py`)
is a Claude call using its general market knowledge, not a live query
against those sites. It's always labeled `Est.` in the digest and the DB's
`source_note` says `AI-estimated`, so it's never confused with a stated
salary. Swap `_llm_market_estimate()` for a real API call if/when one of
those becomes available — the cache contract (keyed by company + normalized
role title + location) stays the same.

FX conversion uses a free live rate lookup (open.er-api.com) with a static
fallback from `config.yaml -> salary.fallback_fx_to_inr` if that's
unreachable; the digest header always states which was used and when.

## Application status tracking

`postings.status` moves `new -> matched -> queued_for_review` automatically.
`cv_generated`, `applied`, `rejected` are set manually once you act on a
digest entry — this pipeline never sets them itself. From a Python shell:

```python
from settings import load_config, data_path
from store.db import open_store

cfg = load_config()
with open_store(data_path(cfg["output"]["db_path"])) as store:
    store.set_status("<url_hash from the digest or DB>", "applied")
```

## GitHub Actions

`.github/workflows/job-pipeline.yml` runs `main.py` daily (03:00 UTC) and
commits `data/jobs.db` + `digests/` back to the repo so state and history
persist across runs. Set repo secrets: `ANTHROPIC_API_KEY` (required),
`RAPIDAPI_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` (optional, only if those
sources are enabled). Trigger a manual run anytime from the Actions tab
(`workflow_dispatch`).

## Constraints this pipeline honors

- No auto-submission of applications, ever — output is always a digest.
- No CV generation — that's a separate, manual Claude.ai Project step.
- No direct LinkedIn scraping (JSearch/Adzuna reach LinkedIn-sourced
  listings only through their own licensed APIs).
- Every scraper-style source is rate-limited and disk-cached; see
  `ingest/base.py`'s `RateLimiter`/`DiskCache`. Check each target site's
  `robots.txt`/ToS before enabling a new scraper module.
