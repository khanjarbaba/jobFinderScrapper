"""Shared types + HTTP helpers for every ingest/ source module.

Every source module exposes one function, `fetch(cfg: dict) -> list[JobPosting]`,
and must never raise on a transient failure — log and return whatever it has
(or an empty list) so one broken source never takes the whole run down.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("ingest")

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (compatible; job-pipeline/1.0; "
    "+https://github.com/) personal-job-search-bot"
)


@dataclass
class JobPosting:
    """Normalized shape every ingest module must return."""

    title: str
    company: str
    location: str
    description: str
    url: str
    source: str
    posted_date: Optional[str] = None  # ISO 8601 date string if known
    visa_sponsorship: Optional[bool] = None  # None = unknown/not stated
    remote_type: Optional[str] = None  # "remote" | "hybrid" | "onsite" | None
    employment_type: Optional[str] = None  # "full_time" | "contract" | "internship" | ...
    salary_text: Optional[str] = None  # raw stated salary/comp text, if present in the JD
    raw: dict[str, Any] = field(default_factory=dict)  # original payload, for debugging

    def url_hash(self) -> str:
        return hashlib.sha256(self.url.strip().lower().encode("utf-8")).hexdigest()


class RateLimiter:
    """Simple sleep-based limiter: at most `per_seconds` seconds between calls."""

    def __init__(self, min_interval_seconds: float = 1.0):
        self.min_interval = min_interval_seconds
        self._last_call = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


class DiskCache:
    """Flat-file JSON cache keyed by a stable hash of (namespace, key).

    Used by scraper-style sources so a single run never has to re-hit a
    site that's slow, rate-limited, or flaky more than once per TTL.
    """

    def __init__(self, namespace: str, ttl_seconds: int = 6 * 3600):
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        self.dir = CACHE_DIR / namespace
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.dir / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.ttl_seconds:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        path.write_text(json.dumps(value), encoding="utf-8")


_RETRYABLE = (requests.ConnectionError, requests.Timeout)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(_RETRYABLE),
)
def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: int = 20) -> requests.Response:
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)
    resp = requests.get(url, params=params, headers=merged_headers, timeout=timeout)
    resp.raise_for_status()
    return resp


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def strip_html(text: str | None) -> str:
    """Cheap HTML->text for JD bodies that arrive as markup (RemoteOK, RSS, etc.)."""
    if not text:
        return ""
    text = text.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n") \
        .replace("<br />", "\n").replace("</li>", "\n").replace("</div>", "\n")
    text = _TAG_RE.sub("", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ") \
        .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def extract_main_text(html: str) -> str:
    """Generic HTML->text for full job-detail pages (used by RSS fallback and
    scraper sources). Strips nav/header/footer/script chrome, keeps line breaks.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard dependency, but fail soft
        return strip_html(html)

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def matches_any_keyword(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in keywords)


def stub_source(name: str, reason: str) -> list[JobPosting]:
    """Placeholder for a source not yet implemented. Returns [] so main.py's
    orchestration loop treats every configured source uniformly instead of
    branching on which ones exist yet.
    """
    logger.info("[%s] not yet implemented, skipping (%s)", name, reason)
    return []


def safe_fetch(source_name: str, fn, *args, **kwargs) -> list[JobPosting]:
    """Wraps a source's fetch() so one source raising never breaks main.py."""
    try:
        result = fn(*args, **kwargs)
        logger.info("[%s] fetched %d postings", source_name, len(result))
        return result
    except Exception as exc:  # noqa: BLE001 - intentionally broad, see docstring
        logger.warning("[%s] fetch failed, skipping this source for this run: %s",
                        source_name, exc)
        return []
