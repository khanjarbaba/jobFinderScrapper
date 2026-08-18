"""Populates a salary_display string (+ INR min/max where possible) for every
matched posting, always - never left blank, so the digest stays scannable.

Priority order per posting:
  1. Stated salary text pulled straight from the JD by ingest/ (job.salary_text)
     -> parsed with a best-effort regex, "ballpark ok" per spec.
  2. A cached secondary-lookup estimate keyed by (company, normalized role
     title, location) - the same company/role combo recurs across postings,
     so we don't want to re-spend a call on it.
  3. A fresh secondary-lookup estimate. We don't hold API keys for Glassdoor/
     AmbitionBox/Levels.fyi/Naukri's salary insights, so this is a Claude
     call using its general market knowledge as a stand-in - clearly labeled
     "Est." either way. If real access to one of those sites is wired up
     later, swap _llm_market_estimate() for a real lookup and keep the same
     cache contract.
  4. "Not disclosed" if nothing above produced a number.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic

from ingest.base import http_get
from ingest.base import JobPosting
from settings import env

logger = logging.getLogger("salary_enrichment")

_client: Optional[Anthropic] = None
_fx_rates_cache: Optional[dict[str, float]] = None
_fx_rates_label: Optional[str] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = env("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
        _client = Anthropic(api_key=api_key)
    return _client


@dataclass
class SalaryResult:
    salary_display: str
    salary_min_inr: Optional[float] = None
    salary_max_inr: Optional[float] = None
    is_estimated: bool = False
    source_note: Optional[str] = None


def get_fx_rates(cfg: dict) -> tuple[dict[str, float], str]:
    """Returns ({currency: rate_to_inr}, human-readable label for the digest).

    Tries a free, no-auth FX API first; falls back to config.yaml's static
    rates (and says so in the label) if that lookup fails for any reason.
    """
    global _fx_rates_cache, _fx_rates_label
    if _fx_rates_cache is not None:
        return _fx_rates_cache, _fx_rates_label

    try:
        resp = http_get("https://open.er-api.com/v6/latest/USD", timeout=10)
        payload = resp.json()
        rates = payload["rates"]
        usd_to_inr = rates["INR"]
        computed = {
            "INR": 1.0,
            "USD": usd_to_inr,
            "EUR": usd_to_inr / rates["EUR"],
            "GBP": usd_to_inr / rates["GBP"],
        }
        label = f"live FX as of {payload.get('time_last_update_utc', 'unknown time')}"
        _fx_rates_cache, _fx_rates_label = computed, label
        return computed, label
    except Exception as exc:  # noqa: BLE001
        logger.info("live FX lookup failed (%s), using config fallback rates", exc)
        fallback = dict(cfg["salary"]["fallback_fx_to_inr"])
        fallback["INR"] = 1.0
        label = "fallback static FX rates from config.yaml (live lookup unavailable)"
        _fx_rates_cache, _fx_rates_label = fallback, label
        return fallback, label


_LPA_RE = re.compile(
    r"(?P<min>[\d.]+)\s*(?:-|–|to)\s*(?P<max>[\d.]+)\s*LPA", re.IGNORECASE
)
_CURRENCY_RANGE_RE = re.compile(
    r"(?P<symbol>₹|Rs\.?|INR|\$|USD|€|EUR|£|GBP)\s*"
    r"(?P<min>[\d,.]+)\s*(?:k|K)?\s*(?:-|–|to)\s*"
    r"(?P<symbol2>₹|Rs\.?|INR|\$|USD|€|EUR|£|GBP)?\s*(?P<max>[\d,.]+)\s*(?P<k>k|K)?",
)
_SYMBOL_TO_CURRENCY = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}


def _parse_stated_salary(salary_text: str, fx_rates: dict[str, float]) -> Optional[SalaryResult]:
    if not salary_text:
        return None

    lpa_match = _LPA_RE.search(salary_text)
    if lpa_match:
        min_lpa = float(lpa_match.group("min"))
        max_lpa = float(lpa_match.group("max"))
        return SalaryResult(
            salary_display=f"₹{min_lpa:g}-{max_lpa:g} LPA, stated",
            salary_min_inr=min_lpa * 100_000,
            salary_max_inr=max_lpa * 100_000,
            is_estimated=False,
            source_note="stated in JD",
        )

    match = _CURRENCY_RANGE_RE.search(salary_text)
    if match:
        symbol = match.group("symbol").lower().rstrip(".")
        currency = _SYMBOL_TO_CURRENCY.get(symbol)
        if currency and currency in fx_rates:
            try:
                min_val = float(match.group("min").replace(",", ""))
                max_val = float(match.group("max").replace(",", ""))
            except ValueError:
                return None
            if match.group("k"):
                min_val *= 1000
                max_val *= 1000
            rate = fx_rates[currency]
            min_inr, max_inr = min_val * rate, max_val * rate
            lpa_min, lpa_max = min_inr / 100_000, max_inr / 100_000
            return SalaryResult(
                salary_display=f"₹{lpa_min:.1f}-{lpa_max:.1f} LPA, stated (converted from {currency})",
                salary_min_inr=min_inr,
                salary_max_inr=max_inr,
                is_estimated=False,
                source_note=f"stated in JD as {currency}, converted to INR",
            )

    return None


def _normalize_cache_key(company: str, title: str, location: str) -> str:
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().lower())

    return f"{norm(company)}|{norm(title)}|{norm(location)}"


def _llm_market_estimate(job: JobPosting, cfg: dict) -> Optional[SalaryResult]:
    client = _get_client()
    model = cfg["matching"]["model"]
    prompt = f"""Give a best-effort estimate of ANNUAL total compensation, in INR \
lakhs per annum (LPA), for this role. Base it on general market knowledge of \
what sites like Glassdoor, AmbitionBox, Levels.fyi, or Naukri's salary \
insights would show for a comparable title/company-type/location. If you \
genuinely have no reasonable basis (e.g. a very obscure or brand-new \
company), say so instead of guessing.

Respond with ONLY a JSON object, no prose:
{{"min_lpa": <number or null>, "max_lpa": <number or null>, \
"basis": "<one short phrase, e.g. 'similar B2B SaaS PM roles in Bangalore'>"}}

Role title: {job.title}
Company: {job.company}
Location: {job.location}
Remote type: {job.remote_type}
"""
    try:
        response = client.messages.create(
            model=model, max_tokens=200, messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        parsed = json.loads(match.group(0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("salary estimate call failed for %s: %s", job.title, exc)
        return None

    min_lpa, max_lpa = parsed.get("min_lpa"), parsed.get("max_lpa")
    if min_lpa is None or max_lpa is None:
        return None

    basis = parsed.get("basis", "AI market estimate")
    return SalaryResult(
        salary_display=f"Est. ₹{min_lpa:g}-{max_lpa:g} LPA",
        salary_min_inr=float(min_lpa) * 100_000,
        salary_max_inr=float(max_lpa) * 100_000,
        is_estimated=True,
        source_note=f"AI-estimated ({basis}) - not a live Glassdoor/AmbitionBox/Levels.fyi query",
    )


def enrich_salary(job: JobPosting, cfg: dict, store) -> SalaryResult:
    fx_rates, _ = get_fx_rates(cfg)

    stated = _parse_stated_salary(job.salary_text or "", fx_rates)
    if stated:
        return stated

    cache_key = _normalize_cache_key(job.company, job.title, job.location)
    ttl_days = cfg["salary"]["cache_ttl_days"]
    cached = store.get_salary_lookup_cache(cache_key)
    if cached is not None:
        from datetime import datetime, timezone
        looked_up_at = datetime.fromisoformat(cached["looked_up_at"])
        age_days = (datetime.now(timezone.utc) - looked_up_at).days
        if age_days <= ttl_days:
            return SalaryResult(
                salary_display=cached["salary_display"],
                salary_min_inr=cached["salary_min_inr"],
                salary_max_inr=cached["salary_max_inr"],
                is_estimated="Est." in cached["salary_display"],
                source_note="cached estimate",
            )

    estimate = _llm_market_estimate(job, cfg)
    if estimate:
        store.set_salary_lookup_cache(
            cache_key, estimate.salary_display, estimate.salary_min_inr, estimate.salary_max_inr,
        )
        return estimate

    not_disclosed = SalaryResult(salary_display="Not disclosed")
    store.set_salary_lookup_cache(cache_key, not_disclosed.salary_display, None, None)
    return not_disclosed
