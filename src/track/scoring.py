"""Deduplication, underpriced scoring, and per-source statistics.

Everything here is pure: it takes findings and returns numbers, so the
interesting behaviour is testable without a database or a scout.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from collections import defaultdict
from urllib.parse import urlsplit

from .models import Finding, SourceStat


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

    1.0 = cheaper than everything else on record, 0.0 = the priciest. The
    history is the run's opening snapshot, taken once and not extended while
    the run scores -- otherwise two identical runs would score differently
    purely because their scouts returned in a different order. 0.5 when there
    is no history to judge against yet.
    """
    if not history:
        return 0.5
    beats_or_ties = sum(1 for h in history if h >= price)
    return beats_or_ties / len(history)


def source_stats(findings: list[Finding]) -> list[SourceStat]:
    """Summarise who has actually been producing the cheap listings.

    Expects one finding per distinct listing (see `Store.latest_findings`);
    handing it raw rows would weight a source by how often its listings were
    re-seen rather than by how many it has.
    """
    # Keyed by (source, currency), not by source alone: a source quoting in
    # two currencies has two honest sets of numbers, and a median taken across
    # them is a number that describes nothing.
    by_source: dict[tuple[str, str | None], list[Finding]] = defaultdict(list)
    for finding in findings:
        by_source[(finding.source, finding.currency)].append(finding)

    stats: list[SourceStat] = []
    for (name, currency), group in by_source.items():
        prices = [f.price for f in group if f.price is not None]
        scores = [f.score for f in group if f.score is not None]
        stats.append(
            SourceStat(
                name=name,
                listings=len(group),
                priced=len(prices),
                cheapest=min(prices) if prices else None,
                median=statistics.median(prices) if prices else None,
                best_score=max(scores) if scores else None,
                currency=currency,
            )
        )
    # Sources with no usable price sort last rather than first: an unpriced
    # source is not a cheap one, it is an unreadable one. Within that, group by
    # currency before price -- the ordering is only meaningful inside one.
    stats.sort(
        key=lambda s: (
            s.cheapest is None,
            s.currency or "",
            s.cheapest if s.cheapest is not None else 0.0,
        )
    )
    return stats
