"""Deduplication, underpriced scoring, and per-source statistics.

Everything here is pure: it takes findings and returns numbers, so the
interesting behaviour is testable without a database or a scout.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable
from urllib.parse import urlsplit

from .models import Finding, SourceStat


def url_basis(url: str) -> str:
    """The identifying part of a listing URL: host + path, query stripped.

    The query string is where trackers and session ids live, so two links to
    the same listing differ there and nowhere else.
    """
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}".lower().rstrip("/")


def title_basis(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def index_url_bases(listings: Iterable[tuple[str | None, str]]) -> set[str]:
    """URL bases that are search or category pages rather than listings.

    Takes (url, title) pairs *from a single run* and returns the bases that
    came back attached to more than one distinct title. A product page yields
    one title per run; a search page yields one per result, so this separates
    them by observation rather than by guessing at URL shape.

    Measured on the 9 runs in the live database: 147 bases had exactly one
    title in a run, 7 had more, and all 7 were visibly index pages (two
    `?pretraga=` searches, two category listings, one bare host). No product
    page was misclassified.
    """
    titles: dict[str, set[str]] = defaultdict(set)
    for url, title in listings:
        if url:
            titles[url_basis(url)].add(title_basis(title))
    return {basis for basis, seen in titles.items() if len(seen) > 1}


def dedup_key(
    source: str, title: str, url: str | None, index_bases: frozenset[str] | set[str] = frozenset()
) -> str:
    """Stable key identifying "the same listing" across runs.

    Prefers the URL, and falls back to a normalized title when there isn't
    one -- or when the URL is a known index page, which is the load-bearing
    part. A scout that answers with the search-results URL for every hit
    hands back ten cards sharing one path; keyed on that path they become one
    listing and nine of them stop existing for every query downstream. That
    happened for real: ten GPUs, prices 1 to 1100 EUR, collapsed onto
    `kupujemprodajem.com/.../pretraga`, and only the last survived
    `latest_findings`.

    Titling is not the default because it breaks the opposite case: one real
    product page came back under four slightly different title strings across
    four runs, and keying those by title would turn one listing with a price
    history into four listings with none.
    """
    if url and url_basis(url) not in index_bases:
        basis = url_basis(url)
    else:
        basis = title_basis(title)
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
