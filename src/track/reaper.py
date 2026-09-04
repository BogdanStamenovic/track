"""Retires listings that are gone, sold, or made redundant by a better find.

Runs after every research cycle. Two rules govern it and neither is
negotiable:

**Retirement is a marking, never a deletion.** A retired listing keeps every
sighting row it ever had, so "what did that cost in July" stays answerable
after the advert is gone, and a listing that turns up alive again is simply
un-retired.

**A failed fetch is not proof a listing is dead.** Timeouts, rate limits and
anti-bot walls are the normal weather of scraping third-party marketplaces,
and treating one as a death certificate would quietly empty the database of
exactly the sources that block hardest. So a listing is retired on a positive
removal signal -- the page itself saying sold, expired or 404 -- or on
`FAILURES_BEFORE_RETIRING` separate inconclusive checks, and the row records
which of the two it was. A site-level block is recorded and counts toward
neither: a 403 is evidence about the site and none whatsoever about the
listing behind it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import scouts
from .errors import ScoutError
from .models import Finding
from .scoring import Market, tokenize
from .store import Store

# How many listings one run re-checks. This is one extra scout on top of the
# five a cycle already spends, and it works through the back catalogue
# least-recently-checked first, so nothing is skipped forever -- a 135-listing
# assignment is fully swept in about a dozen runs.
MAX_CHECKS_PER_RUN = 12

# Inconclusive checks before a listing is retired as gone. Three, because a
# single unreachable fetch is routine and two consecutive ones are still
# plausibly one bad afternoon; a listing has to be unreachable across three
# separate runs, having also failed to turn up in any of their scouting.
FAILURES_BEFORE_RETIRING = 3

# Superseding is held to a much higher bar than price comparison, and the two
# use different similarity measures on purpose.
#
# The reference price wants recall, so it uses the overlap coefficient --
# shared weight over the *shorter* title. That measure ignores whatever the
# longer title says beyond the overlap, which is fine for a median and fatal
# for a claim that one listing replaces another: a listing called simply
# "Thinkpad" overlaps perfectly into every ThinkPad on the site and would
# "supersede" a P52 workstation. So supersession uses the symmetric measure
# instead, which charges for the unshared half.
#
# Measured on 162 live listings, retiring on "a cheaper comparable exists":
#
#   rule                                       retired    example replacement
#   overlap >= 0.45, 15% cheaper                   48%    ProBook 650 G2 for a 450 G7
#   overlap >= 0.90, 20% cheaper                   18%    "Thinkpad" for a P52 workstation
#   symmetric >= 0.75, 10% cheaper                  0%    --
#
# The broad rules retire half the market on replacements a person would
# reject at a glance. The strict rule almost never fires, and that is the
# honest finding rather than a failure to tune: what high title similarity
# actually turns up in this data is duplicates at the *same* price, which is
# bookkeeping, not a better deal.
SUPERSEDE_SIMILARITY = 0.75
SUPERSEDE_MARGIN = 0.10

# A "comparable" priced under a quarter of the listing it would replace is a
# data error, not a bargain -- an RTX 3090 was posted at 1 EUR, and without
# this it would retire every real 3090 on the board.
SUPERSEDE_FLOOR = 0.25


@dataclass(frozen=True, slots=True)
class ReapOutcome:
    """What the reaper did, in the terms the run summary needs."""

    checked: int = 0
    retired_gone: list[str] = field(default_factory=list)
    retired_superseded: list[str] = field(default_factory=list)
    blocked: int = 0  # checks a site refused; evidence about the site, not the listing
    inconclusive: int = 0  # checks that established nothing and did not count as a block
    cost_usd: float = 0.0

    @property
    def retired(self) -> int:
        return len(self.retired_gone) + len(self.retired_superseded)


def _symmetric_similarity(market: Market, a: str, b: str) -> float:
    """Shared distinctive weight over the *union* of the two titles.

    The measure `Market` uses for pricing divides by the shorter title, which
    is what lets a three-word title match a specification-laden one. Here that
    is exactly the wrong behaviour, so this charges for both halves.
    """
    tokens_a, tokens_b = frozenset(tokenize(a)), frozenset(tokenize(b))
    union = market.weight_of(tokens_a | tokens_b)
    return market.weight_of(tokens_a & tokens_b) / union if union else 0.0


def find_superseded(
    market: Market, live: list[Finding]
) -> list[tuple[Finding, Finding]]:
    """Pairs of (listing to retire, the near-identical listing that beat it).

    Only near-identical listings, and only a materially better price. Anything
    looser retires listings a person would still want to see -- an EliteBook
    855 G8 in Grade A is not superseded by the same model in Grade C being
    7% cheaper, and the titles say so.
    """
    by_key = {f.dedup_key: f for f in live}
    pairs: list[tuple[Finding, Finding]] = []
    for finding in live:
        if finding.price is None:
            continue
        reference = market.reference(finding.dedup_key, finding.title, finding.currency)
        if reference is None:
            continue
        better = [
            peer
            for key, _ in reference.peers
            if (peer := by_key.get(key)) is not None
            and peer.price is not None
            and peer.price <= finding.price * (1 - SUPERSEDE_MARGIN)
            and peer.price >= finding.price * SUPERSEDE_FLOOR
            and _symmetric_similarity(market, finding.title, peer.title)
            >= SUPERSEDE_SIMILARITY
        ]
        if better:
            pairs.append((finding, min(better, key=lambda f: f.price or 0.0)))
    return pairs


def reap(
    store: Store,
    assignment_id: str,
    market: Market,
    *,
    seen_keys: set[str],
    check: Callable[[list[str]], tuple[list[scouts.ListingCheck], float]] | None = None,
    warn: Callable[[str], None] = lambda _m: None,
) -> ReapOutcome:
    """Re-check what this run did not see, and retire what has gone or been beaten.

    `check` resolves at call time rather than as a default argument value, so
    that replacing `scouts.check_listings` actually replaces it. Binding it in
    the signature gave the test suite a real `claude -p` per engine test while
    appearing to be stubbed.
    """
    check = check or scouts.check_listings
    due = store.listings_due_for_check(
        assignment_id, exclude=seen_keys, limit=MAX_CHECKS_PER_RUN
    )
    # One listing per URL. The store already refuses to hand over listings
    # that only have a search-page URL, so anything left sharing one would be
    # a listing whose check result belongs to something else.
    by_url: dict[str, Finding] = {}
    for finding in due:
        if finding.url and finding.url not in by_url:
            by_url[finding.url] = finding

    gone: list[str] = []
    blocked = 0
    inconclusive = 0
    cost = 0.0
    if by_url:
        try:
            results, cost = check(list(by_url))
        except ScoutError as exc:
            # The whole batch failing says nothing about any listing in it.
            warn(f"listing re-check failed: {exc}")
            results = []
        # A URL nothing came back for is unknown, never gone. Silence is the
        # commonest way a batch check goes wrong -- a scout that ran out of
        # budget halfway, or answered about four of twelve -- and it must not
        # read as evidence that eight listings sold. Filled in here rather
        # than in the scout so the guarantee holds whoever does the checking.
        answered = {r.url for r in results if r.url in by_url}
        results = list(results) + [
            scouts.ListingCheck(url=url, state="unknown", note="no answer came back for this URL")
            for url in by_url
            if url not in answered
        ]
        for result in results:
            checked = by_url.get(result.url)
            if checked is None:
                continue
            finding = checked
            if result.state == "live":
                store.record_check(
                    assignment_id, finding.dedup_key, conclusive=True, note=result.note
                )
            elif result.state == "gone":
                store.retire(
                    assignment_id,
                    finding.dedup_key,
                    reason="gone",
                    note=result.note or "the listing page says it is no longer on offer",
                )
                gone.append(finding.title)
            elif result.state == "blocked":
                blocked += 1
                store.record_block(
                    assignment_id,
                    finding.dedup_key,
                    f"could not check: {result.note or 'the site refused the request'}",
                )
            else:
                inconclusive += 1
                failures = store.record_check(
                    assignment_id, finding.dedup_key, conclusive=False, note=result.note
                )
                if failures >= FAILURES_BEFORE_RETIRING:
                    store.retire(
                        assignment_id,
                        finding.dedup_key,
                        reason="gone",
                        note=f"unreachable on {failures} separate checks, never confirmed sold",
                    )
                    gone.append(finding.title)

    superseded: list[str] = []
    for loser, winner in find_superseded(market, store.live_listings(assignment_id)):
        store.retire(
            assignment_id,
            loser.dedup_key,
            reason="superseded",
            note=f"same listing found at {winner.price:,.2f} {winner.currency or ''}".strip(),
            superseded_by=winner.dedup_key,
        )
        superseded.append(loser.title)

    return ReapOutcome(
        checked=len(by_url),
        retired_gone=gone,
        retired_superseded=superseded,
        blocked=blocked,
        inconclusive=inconclusive,
        cost_usd=cost,
    )
