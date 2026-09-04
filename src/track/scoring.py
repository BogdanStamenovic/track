"""Deduplication, mispricing scoring, and per-source statistics.

Everything here is pure: it takes findings and returns numbers, so the
interesting behaviour is testable without a database or a scout.

The scoring question is "is this priced below what this thing goes for",
which needs a reference price for the *comparable* rather than a ranking of
every number the assignment has ever seen. `Market` builds that reference out
of the other listings on record; `underpriced_score` is the fallback for when
there is no comparable to build one from.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
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

    The source name is part of the key only in the title fallback. A URL
    already names the site, and folding in what a scout decided to *call* it
    that run splits one listing in two the moment the name drifts -- which it
    does: the same pages came back under "Konovo.rs" and "Konovo.rs (formerly
    Polovnilaptop.rs)", and under "polovnilaptopovi.rs" and
    "polovnilaptopovi.rs (Restart Laptopovi)". Five listings on record were
    duplicates of that kind. Without a URL the source is all that separates
    two sites using the same title, so there it stays.
    """
    if url and url_basis(url) not in index_bases:
        return hashlib.sha1(url_basis(url).encode()).hexdigest()[:16]
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


# -- comparables -----------------------------------------------------------

# How much of the shorter title's distinctive weight two listings must share
# before one is a price reference for the other. Chosen on the two measures
# that need no labels, over the 152 priced listings on record:
#
#   threshold   comparable coverage   rho(score, price)
#   0.40                    94.7%               -0.067
#   0.45                    92.8%               +0.011
#   0.50                    87.5%               +0.107
#
# The second column is the one that matters. A cheapness percentile scores
# -0.546 there -- it is very nearly a restatement of "small number" -- and a
# valuation should sit near zero, because an expensive thing can be the
# better deal. 0.45 is where that crosses. Against 29 listings in six
# hand-verified comparable groups it also happens to peak (+0.871 against
# +0.777 for cheapness), but that set is small and hand-picked, so it
# confirms the choice rather than making it.
COMPARABLE_THRESHOLD = 0.45

# Deviation is weighted n/(n+K): a reference drawn from one peer moves the
# score half as far as the arithmetic says, and the discount is trusted in
# full only once several listings agree. Measured: injecting twelve unrelated
# RTX 3090 listings into the history moved scores by at most 0.31 unweighted
# and 0.19 weighted, for a cost of 0.01 against the hand-verified set, which
# is inside the noise of a 29-item sample.
CONFIDENCE_K = 1.0

# How many peers to keep as the recorded evidence for one finding. The whole
# neighbourhood can be dozens wide for a vague title; a handful of the most
# similar is what answers "why was this recommended".
MAX_PEERS_RECORDED = 8

# Inverse document frequency is computed against at least this many listings,
# even when fewer exist. On a small snapshot the weights inverted: the tokens
# that identify a model recur across its comparables and so look *common*,
# while a cooler variant nobody else uses ("VENTUS") looks rare and outweighs
# them. Pretending the corpus is bigger compresses that gap. It is free on a
# grown assignment -- the numbers on the 152 listings on record are identical
# with the floor at 0, 30, 60, 100 or 150 -- and it roughly doubles small-run
# recall: sampling the GPU assignment down to six listings, the share of the
# RTX 3060 group that still finds a comparable goes from 30% to 59%, and at
# twenty listings from 90% to 98%. Anywhere in 30..100 performs the same;
# below about fourteen listings comparables are thin whatever you do, which
# is what the cheapness fallback is for.
MIN_IDF_CORPUS = 60

_CAP = re.compile(r"\b(\d+)\s*(gb|tb|mb)\b")
_RUN = re.compile(r"[a-z]+|\d+")

# Words that say nothing about which product this is: units, condition
# adjectives in both languages of the market this was built for, and the
# vendor boilerplate that every listing in a category carries.
_NOISE = frozenset({
    "laptop", "notebook", "ram", "ssd", "hdd", "nvme", "gb", "tb", "mb", "inch", "novo",
    "novi", "nov", "kao", "odlican", "odlicno", "top", "akcija", "br", "grade", "the",
    "and", "with", "za", "na", "sa", "gaming", "oc", "graphics", "geforce", "nvidia", "amd",
    "intel", "core", "ips", "fhd", "touch", "touchscreen", "display", "screen", "bat",
    "premium", "plus", "pro", "max", "edition", "out", "of", "stock",
})


def tokenize(title: str) -> list[str]:
    """Words, plus capacities, plus the letter/digit runs inside mixed tokens.

    The last part is load-bearing. "PH-RTX3060-12G-V2" and "MSI GeForce RTX
    3060 12GB" are the same card and share nothing at all until `rtx3060` also
    emits `rtx` and `3060`; adding the split raised agreement with the
    hand-verified comparable groups from +0.764 to +0.881. The whole token is
    kept as well, so an exact model code still counts for more than its parts.
    """
    text = title.lower()
    caps = [f"{n}{unit}" for n, unit in _CAP.findall(text)]
    text = _CAP.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if len(w) > 1 and w not in _NOISE]
    parts = [
        run
        for w in words
        if not (w.isalpha() or w.isdigit())
        for run in _RUN.findall(w)
        if len(run) > 1 and run not in _NOISE
    ]
    return words + caps + parts


@dataclass(frozen=True, slots=True)
class Comparable:
    """One listing, as the market snapshot sees it."""

    key: str  # dedup_key
    title: str
    price: float
    currency: str | None


@dataclass(frozen=True, slots=True)
class Reference:
    """What comparable listings say this thing goes for."""

    price: float
    peers: tuple[tuple[str, float], ...]  # (dedup_key, similarity), most similar first

    @property
    def n(self) -> int:
        return len(self.peers)


class Market:
    """A frozen snapshot of what is on sale, for drawing reference prices.

    Frozen because a run must not score differently depending on which scout
    returned first: the snapshot is built once from the assignment's history
    *and* the whole of this run's haul, before anything is scored. Including
    the run's own findings is the point rather than a shortcut -- five RTX
    3060s at 230 to 440 EUR arrived in a single run of a brand-new assignment,
    and against history alone there was nothing to compare them with.
    """

    def __init__(self, comparables: Iterable[Comparable]) -> None:
        self._by_currency: dict[str | None, list[Comparable]] = defaultdict(list)
        seen: set[str] = set()
        for c in comparables:
            if c.key in seen:
                continue
            seen.add(c.key)
            self._by_currency[c.currency].append(c)
        self._tokens = {
            c.key: frozenset(tokenize(c.title))
            for group in self._by_currency.values()
            for c in group
        }
        self._weights = self._idf()

    def _idf(self) -> dict[str, float]:
        total = max(len(self._tokens), MIN_IDF_CORPUS)
        counts: dict[str, int] = defaultdict(int)
        for tokens in self._tokens.values():
            for token in tokens:
                counts[token] += 1
        self._unseen = math.log(total)
        return {token: math.log(total / n) for token, n in counts.items()}

    def _weight(self, tokens: Iterable[str]) -> float:
        return sum(self._weights.get(t, self._unseen) for t in tokens)

    def weight_of(self, tokens: Iterable[str]) -> float:
        """This snapshot's distinctiveness weighting, for a second opinion.

        The reaper needs a *symmetric* similarity where this class uses an
        asymmetric one, and it has to be weighted against the same corpus to
        be comparable, so the weighting is exposed rather than recomputed.
        """
        return self._weight(tokens)

    def _containment(self, a: frozenset[str], b: frozenset[str]) -> float:
        """Shared distinctive weight over the *shorter* title's.

        Not Jaccard. Dividing by the union punishes length asymmetry, and
        scout titles are wildly asymmetric -- "MSI GeForce RTX 3060 12GB
        VENTUS 2X" against "Nvidia RTX 3060 12GB (appears underpriced vs other
        3060 12GB listings on same search, EUR 245-440)" are the same card,
        and the commentary in the second inflates the union enough for Jaccard
        to score them apart. Switching to containment took comparable coverage
        from 73% to 90%.
        """
        smaller = min(self._weight(a), self._weight(b))
        return self._weight(a & b) / smaller if smaller else 0.0

    def reference(self, key: str, title: str, currency: str | None) -> Reference | None:
        """What this listing's comparables are asking, or None if it has none.

        `key` is excluded so a listing is never compared against itself or
        against its own earlier sighting.
        """
        tokens = frozenset(tokenize(title))
        peers: list[tuple[str, float, float]] = []
        for c in self._by_currency.get(currency, ()):
            if c.key == key:
                continue
            score = self._containment(tokens, self._tokens[c.key])
            if score >= COMPARABLE_THRESHOLD:
                peers.append((c.key, score, c.price))
        if not peers:
            return None
        peers.sort(key=lambda p: -p[1])
        return Reference(
            price=statistics.median([p[2] for p in peers]),
            peers=tuple((k, round(s, 4)) for k, s, _ in peers[:MAX_PEERS_RECORDED]),
        )


def mispricing_score(price: float, reference: Reference) -> float:
    """How far below what its comparables ask this listing is priced.

    0.5 is "priced at what this goes for", 1.0 is half that or less, 0.0 is
    half again as much or more. Linear in the discount and clamped, because a
    reader has to be able to hold what the number means: 0.80 is thirty
    percent under the going rate.

    The deviation is weighted by how many peers backed the reference -- see
    CONFIDENCE_K -- so a one-peer reference states half of what it saw.
    """
    if reference.price <= 0:
        return 0.5
    discount = (reference.price - price) / reference.price
    weight = reference.n / (reference.n + CONFIDENCE_K)
    return max(0.0, min(1.0, 0.5 + discount * weight))


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
