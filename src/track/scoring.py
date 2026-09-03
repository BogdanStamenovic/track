"""Deduplication and underpriced scoring for track findings."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit


def dedup_key(source: str, title: str, url: str | None) -> str:
    """Stable key identifying "the same listing" across runs.

    Prefers the URL (host + path, query stripped -- trackers and session ids
    live in the query string) and falls back to a normalized title when a
    scout doesn't return one.
    """
    if url:
        parts = urlsplit(url)
        basis = f"{parts.netloc}{parts.path}".lower().rstrip("/")
    else:
        basis = re.sub(r"\s+", " ", title.strip().lower())
    return hashlib.sha1(f"{source.strip().lower()}|{basis}".encode()).hexdigest()[:16]


def underpriced_score(price: float, history: list[float]) -> float:
    """How underpriced `price` is against this assignment's price history.

    1.0 = cheapest ever seen for this assignment, 0.0 = priciest. `history`
    is prior prices for the same assignment (this price excluded) -- the
    score is relative to everything ever found for this assignment, not
    just the current run, so a run never retroactively rescored history.
    Returns 0.5 when there isn't enough history yet to judge.
    """
    if not history:
        return 0.5
    beats_or_ties = sum(1 for h in history if h >= price)
    return beats_or_ties / len(history)
