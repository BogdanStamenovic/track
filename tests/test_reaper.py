"""Reaper tests: what gets retired, and -- mostly -- what must not be."""

from __future__ import annotations

import pytest

from track.errors import ScoutError
from track.models import Finding
from track.reaper import (
    FAILURES_BEFORE_RETIRING,
    MAX_CHECKS_PER_RUN,
    find_superseded,
    reap,
)
from track.scoring import Comparable, Market, dedup_key
from track.scouts import ListingCheck
from track.store import Store


def _checker(*results: ListingCheck):
    return lambda urls, **kwargs: (list(results), 0.01)


@pytest.fixture
def assignment(store: Store):
    return store.add_assignment("a laptop", 3600)


def _add(store: Store, assignment_id: str, title: str, price: float, url: str) -> str:
    key = dedup_key("KP", title, url)
    store.add_finding(
        assignment_id, store.start_run(assignment_id), "KP", title, price, "EUR", url, key, 0.5,
        True,
    )
    return key


EMPTY_MARKET = Market([])


# -- positive removal signals --------------------------------------------


def test_a_page_that_says_sold_retires_the_listing(store: Store, assignment) -> None:
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    outcome = reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=_checker(ListingCheck("https://kp/1", "gone", note="oglas je prodat")),
    )

    status = store.listing_status(assignment.id, key)
    assert status.retired_reason == "gone"
    assert "prodat" in status.retired_note
    assert outcome.retired_gone == ["ThinkPad T490"]


def test_retiring_marks_and_never_deletes(store: Store, assignment) -> None:
    """He may want to know what a thing used to cost after it is gone."""
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=_checker(ListingCheck("https://kp/1", "gone")),
    )

    rows = store._conn.execute(
        "SELECT price FROM findings WHERE dedup_key = ?", (key,)
    ).fetchall()
    assert [r["price"] for r in rows] == [300.0]
    assert store.live_listings(assignment.id) == []


# -- a failed fetch is not a death certificate ---------------------------


def test_a_blocked_site_never_retires_a_listing(store: Store, assignment) -> None:
    """A 403 is evidence about the site and none at all about the listing."""
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    for _ in range(FAILURES_BEFORE_RETIRING + 4):
        outcome = reap(
            store, assignment.id, EMPTY_MARKET, seen_keys=set(),
            check=_checker(ListingCheck("https://kp/1", "blocked", note="403 anti-bot wall")),
        )

    status = store.listing_status(assignment.id, key)
    assert status.retired_at is None
    assert status.check_failures == 0
    assert outcome.blocked == 1
    assert "could not check" in status.last_check_note
    assert status.retired_note is None


def test_one_unreachable_check_is_not_enough_to_retire(store: Store, assignment) -> None:
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=_checker(ListingCheck("https://kp/1", "unknown", note="timed out")),
    )

    assert store.listing_status(assignment.id, key).retired_at is None


def test_repeated_unreachable_checks_do_retire_and_say_which(
    store: Store, assignment
) -> None:
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    for _ in range(FAILURES_BEFORE_RETIRING):
        reap(
            store, assignment.id, EMPTY_MARKET, seen_keys=set(),
            check=_checker(ListingCheck("https://kp/1", "unknown")),
        )

    status = store.listing_status(assignment.id, key)
    assert status.retired_reason == "gone"
    assert "never confirmed sold" in status.retired_note


def test_a_url_the_scout_ignored_counts_as_unknown_not_gone(
    store: Store, assignment
) -> None:
    _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    outcome = reap(store, assignment.id, EMPTY_MARKET, seen_keys=set(), check=_checker())

    assert outcome.retired_gone == []
    assert outcome.inconclusive == 1


def test_a_url_the_scout_invented_cannot_retire_anything(
    store: Store, assignment
) -> None:
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=_checker(ListingCheck("https://kp/does-not-exist", "gone")),
    )

    assert store.listing_status(assignment.id, key).retired_at is None


def test_a_failed_batch_retires_nothing(store: Store, assignment) -> None:
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")
    warnings: list[str] = []

    def boom(urls, **kwargs):
        raise ScoutError("scout timed out after 120s")

    outcome = reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(), check=boom, warn=warnings.append
    )

    assert store.listing_status(assignment.id, key).retired_at is None
    assert outcome.retired == 0
    assert warnings


# -- what does and does not get checked ----------------------------------


def test_a_listing_this_run_saw_is_not_re_checked(store: Store, assignment) -> None:
    """It is alive by definition; re-fetching it is the priciest way to learn that."""
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")
    asked: list[list[str]] = []

    reap(
        store, assignment.id, EMPTY_MARKET, seen_keys={key},
        check=lambda urls, **k: (asked.append(urls), ([], 0.0))[1],
    )

    assert asked == []


def test_checks_are_capped_and_work_through_the_backlog(store: Store, assignment) -> None:
    for i in range(MAX_CHECKS_PER_RUN + 5):
        _add(store, assignment.id, f"ThinkPad T{i}", 300.0, f"https://kp/{i}")
    asked: list[list[str]] = []

    def spy(urls, **kwargs):
        asked.append(urls)
        return [ListingCheck(u, "live") for u in urls], 0.0

    reap(store, assignment.id, EMPTY_MARKET, seen_keys=set(), check=spy)
    reap(store, assignment.id, EMPTY_MARKET, seen_keys=set(), check=spy)

    assert len(asked[0]) == MAX_CHECKS_PER_RUN
    # Least recently checked first, so the second pass reaches what the first
    # could not rather than re-checking the same twelve forever.
    assert set(asked[1]) - set(asked[0])


def test_a_retired_listing_that_reappears_is_alive_again(store: Store, assignment) -> None:
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")
    reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=_checker(ListingCheck("https://kp/1", "gone")),
    )
    assert store.listing_status(assignment.id, key).retired_at is not None

    _add(store, assignment.id, "ThinkPad T490", 280.0, "https://kp/1")

    status = store.listing_status(assignment.id, key)
    assert status.retired_at is None
    assert status.check_failures == 0


# -- superseding ---------------------------------------------------------


def _finding(key: str, title: str, price: float) -> Finding:
    return Finding(
        id=0, assignment_id="a", run_id=1, source="KP", title=title, price=price,
        currency="EUR", url=None, dedup_key=key, score=0.5, is_new=True, found_at="t",
    )


def _market_of(*findings: Finding) -> Market:
    return Market(Comparable(f.dedup_key, f.title, f.price, f.currency) for f in findings)


def test_a_near_identical_cheaper_listing_supersedes() -> None:
    dear = _finding("a", "Lenovo ThinkPad T490 i7 16GB 512GB SSD", 300.0)
    cheap = _finding("b", "Lenovo ThinkPad T490 i7 16GB 512GB SSD", 230.0)

    pairs = find_superseded(_market_of(dear, cheap), [dear, cheap])

    assert [(loser.dedup_key, winner.dedup_key) for loser, winner in pairs] == [("a", "b")]


def test_a_vague_title_does_not_supersede_a_specific_one() -> None:
    """"Thinkpad" at 200 EUR must not retire a P52 workstation at 550."""
    specific = _finding("a", "ThinkPad P52 i7 16GB Quadro P2000 WORKSTATION", 550.0)
    vague = _finding("b", "Thinkpad", 200.0)

    assert find_superseded(_market_of(specific, vague), [specific, vague]) == []


def test_a_different_model_does_not_supersede() -> None:
    a = _finding("a", "HP ProBook 450 G7 i5-10210U 16GB 256GB", 53100.0)
    b = _finding("b", "HP ProBook 650 G2 i5 16GB 256GB", 29000.0)

    assert find_superseded(_market_of(a, b), [a, b]) == []


def test_a_marginally_cheaper_variant_does_not_supersede() -> None:
    """Grade A is not superseded by Grade C being 7% cheaper."""
    grade_a = _finding("a", "HP EliteBook 855 G8 Ryzen 3 5400U 16GB 256GB Grade A", 58500.0)
    grade_c = _finding("b", "HP EliteBook 855 G8 Ryzen 3 5400U 16GB 256GB Grade C", 54500.0)

    assert find_superseded(_market_of(grade_a, grade_c), [grade_a, grade_c]) == []


def test_an_impossible_price_does_not_supersede_the_real_listings() -> None:
    """An RTX 3090 was posted at 1 EUR. It is a data error, not a better deal."""
    real = _finding("a", "RTX 3090 24GB Gigabyte Gaming OC", 1100.0)
    error = _finding("b", "RTX 3090 24GB Gigabyte Gaming OC", 1.0)

    assert find_superseded(_market_of(real, error), [real, error]) == []


def test_superseding_records_what_beat_it(store: Store, assignment) -> None:
    dear = _add(store, assignment.id, "Lenovo ThinkPad T490 i7 16GB 512GB SSD", 300.0, "https://kp/1")
    cheap = _add(store, assignment.id, "Lenovo ThinkPad T490 i7 16GB 512GB SSD", 230.0, "https://kp/2")
    live = store.live_listings(assignment.id)
    market = Market(Comparable(f.dedup_key, f.title, f.price, f.currency) for f in live)

    reap(
        store, assignment.id, market, seen_keys={dear, cheap}, check=_checker()
    )

    status = store.listing_status(assignment.id, dear)
    assert status.retired_reason == "superseded"
    assert status.superseded_by == cheap
    assert store.listing_status(assignment.id, cheap).retired_at is None


def test_a_live_check_leaves_no_retirement_note_behind(store: Store, assignment) -> None:
    """The outcome of checking a listing that is fine is not a retirement."""
    key = _add(store, assignment.id, "ThinkPad T490", 300.0, "https://kp/1")

    reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=_checker(ListingCheck("https://kp/1", "live", note="still listed at 300 EUR")),
    )

    status = store.listing_status(assignment.id, key)
    assert status.last_check_note == "still listed at 300 EUR"
    assert status.retired_note is None
    assert status.retired_at is None


def test_a_listing_only_reachable_through_a_search_page_is_not_checked(
    store: Store, assignment
) -> None:
    """The search page renders fine whether or not that item is still on it,
    so "live" would mean nothing and "gone" would retire every listing on it."""
    url = "https://kp.com/graficke/pretraga?keywords=RTX+3060"
    store.register_index_urls(assignment.id, {"kp.com/graficke/pretraga"})
    for title in ("MSI RTX 3060 12GB", "ASUS RTX 3060 12GB"):
        store.add_finding(
            assignment.id, store.start_run(assignment.id), "KP", title, 245.0, "EUR", url,
            dedup_key("KP", title, url, {"kp.com/graficke/pretraga"}), 0.5, True,
        )
    asked: list[list[str]] = []

    reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=lambda urls, **k: (asked.append(urls), ([], 0.0))[1],
    )

    assert asked == []
    assert all(
        store.listing_status(assignment.id, f.dedup_key).retired_at is None
        for f in store.latest_findings(assignment.id)
    )


def test_due_listings_are_never_silently_dropped_for_sharing_a_url(
    store: Store, assignment
) -> None:
    """Twelve listings collapsed to seven URLs once, and the five that shared
    one vanished from the check without a word."""
    for i in range(4):
        _add(store, assignment.id, f"ThinkPad T{i}", 300.0, f"https://kp/{i}")
    asked: list[list[str]] = []

    outcome = reap(
        store, assignment.id, EMPTY_MARKET, seen_keys=set(),
        check=lambda urls, **k: (asked.append(urls), ([], 0.0))[1],
    )

    assert sorted(asked[0]) == [f"https://kp/{i}" for i in range(4)]
    assert outcome.checked == 4
