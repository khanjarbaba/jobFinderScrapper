"""Public entry point for matching - dispatches to the configured backend.

config.yaml -> matching.mode selects:
  "heuristic" - matching/heuristic.py, free, regex/keyword-based, no API
                calls at all.
  "llm"       - matching/llm.py, Claude-scored, catches nuance the
                heuristic backend can't (see matching/heuristic.py's
                docstring for exactly what that gap is).

main.py only ever calls matcher.score_posting() - it doesn't need to know
which backend produced the result, and switching modes is a one-line
config change, nothing else in the pipeline changes.
"""
from __future__ import annotations

from matching import heuristic, llm
from matching.common import MatchResult

__all__ = ["MatchResult", "score_posting"]


def score_posting(job, cfg: dict) -> MatchResult:
    mode = cfg.get("matching", {}).get("mode", "llm")
    if mode == "heuristic":
        return heuristic.score_posting(job, cfg)
    if mode == "llm":
        return llm.score_posting(job, cfg)
    raise ValueError(f"unknown matching.mode {mode!r}, expected 'heuristic' or 'llm'")
