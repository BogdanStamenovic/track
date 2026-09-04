from __future__ import annotations

import pytest

from track.models import Finding
from track.scoring import (
    MAX_PEERS_RECORDED,
    Comparable,
    Market,
    Reference,
    dedup_key,
    index_url_bases,
    mispricing_score,
    source_stats,
    underpriced_score,
)


def _finding(
    source: str = "eBay",
    price: float | None = 100.0,
    score: float | None = 0.5,
    currency: str | None = "USD",
    key: str = "k",
) -> Finding:
    return Finding(
        id=1,
        assignment_id="a",
        run_id=1,
        source=source,
        title="t",
        price=price,
        currency=currency,
        url=None,
        dedup_key=key,
        score=score,
        is_new=True,
        found_at="2026-09-03T00:00:00+00:00",
    )


# -- dedup ---------------------------------------------------------------


def test_query_string_does_not_change_a_listing_identity() -> None:
    a = dedup_key("eBay", "ThinkPad", "https://ebay.com/itm/1?utm_source=x&sid=99")
    b = dedup_key("eBay", "ThinkPad", "https://ebay.com/itm/1")
    assert a == b


def test_trailing_slash_and_case_do_not_change_identity() -> None:
    assert dedup_key("eBay", "t", "https://EBAY.com/itm/1/") == dedup_key(
        "ebay", "t", "https://ebay.com/itm/1"
    )


def test_different_listings_differ() -> None:
    assert dedup_key("eBay", "t", "https://ebay.com/itm/1") != dedup_key(
        "eBay", "t", "https://ebay.com/itm/2"
    )


def test_title_is_the_fallback_when_a_scout_returns_no_url() -> None:
    assert dedup_key("CL", "  ThinkPad   P52 ", None) == dedup_key("CL", "thinkpad p52", None)


def test_same_title_from_different_sources_is_a_different_listing() -> None:
    assert dedup_key("eBay", "ThinkPad", None) != dedup_key("Craigslist", "ThinkPad", None)


# -- scoring -------------------------------------------------------------


def test_no_history_is_neutral() -> None:
    assert underpriced_score(100.0, []) == 0.5


def test_cheapest_ever_scores_one() -> None:
    assert underpriced_score(10.0, [50.0, 80.0, 200.0]) == 1.0


def test_priciest_ever_scores_zero() -> None:
    assert underpriced_score(500.0, [50.0, 80.0, 200.0]) == 0.0


def test_score_is_the_share_of_history_it_beats() -> None:
    assert underpriced_score(100.0, [50.0, 150.0, 200.0, 250.0]) == 0.75


def test_ties_count_as_beaten() -> None:
    assert underpriced_score(100.0, [100.0, 200.0]) == 1.0


# -- source statistics ---------------------------------------------------


def test_stats_summarise_each_source() -> None:
    findings = [
        _finding("eBay", 100.0, 0.4, key="a"),
        _finding("eBay", 300.0, 0.9, key="b"),
        _finding("eBay", 200.0, 0.5, key="c"),
        _finding("Craigslist", 50.0, 0.8, key="d"),
    ]
    stats = {s.name: s for s in source_stats(findings)}

    assert stats["eBay"].listings == 3
    assert stats["eBay"].priced == 3
    assert stats["eBay"].cheapest == 100.0
    assert stats["eBay"].median == 200.0
    assert stats["eBay"].best_score == 0.9
    assert stats["Craigslist"].cheapest == 50.0


def test_cheapest_source_sorts_first() -> None:
    findings = [_finding("eBay", 100.0, key="a"), _finding("Craigslist", 50.0, key="b")]
    assert [s.name for s in source_stats(findings)] == ["Craigslist", "eBay"]


def test_a_source_that_never_yields_a_price_sorts_last_not_first() -> None:
    """A blocked source is unreadable, not cheap -- it must not top the list."""
    findings = [
        _finding("Blocked", None, None, key="a"),
        _finding("eBay", 900.0, 0.1, key="b"),
    ]
    stats = source_stats(findings)

    assert [s.name for s in stats] == ["eBay", "Blocked"]
    assert stats[-1].cheapest is None
    assert stats[-1].price_rate == 0.0


def test_price_rate_reports_partial_readability() -> None:
    findings = [
        _finding("Mixed", 100.0, key="a"),
        _finding("Mixed", None, None, key="b"),
    ]
    (stat,) = source_stats(findings)
    assert stat.listings == 2
    assert stat.priced == 1
    assert stat.price_rate == 0.5


def test_no_findings_means_no_stats() -> None:
    assert source_stats([]) == []


# -- index urls ----------------------------------------------------------


def test_a_search_page_serving_many_titles_is_recognised_as_an_index() -> None:
    listings = [
        ("https://kp.com/graficke/pretraga?keywords=RTX+3060", "MSI RTX 3060 12GB"),
        ("https://kp.com/graficke/pretraga?keywords=RTX+3090", "Zotac RTX 3090 24GB"),
    ]
    assert index_url_bases(listings) == {"kp.com/graficke/pretraga"}


def test_a_product_page_seen_once_per_run_is_not_an_index() -> None:
    listings = [
        ("https://konovo.rs/proizvod/elitebook-855", "HP EliteBook 855 G8"),
        ("https://konovo.rs/proizvod/thinkpad-t490", "Lenovo ThinkPad T490"),
    ]
    assert index_url_bases(listings) == set()


def test_the_same_title_twice_on_one_url_is_not_an_index() -> None:
    """Two sightings of one listing, not two listings sharing a page."""
    listings = [
        ("https://konovo.rs/proizvod/elitebook-855", "HP EliteBook 855 G8"),
        ("https://konovo.rs/proizvod/elitebook-855", "HP  EliteBook  855  G8 "),
    ]
    assert index_url_bases(listings) == set()


def test_listings_on_an_index_url_keep_separate_identities() -> None:
    """The regression that motivated this: ten cards, one search URL, one key."""
    index = {"kp.com/graficke/pretraga"}
    url = "https://kp.com/graficke/pretraga?keywords=RTX+3060"
    a = dedup_key("KP", "MSI RTX 3060 12GB", url, index)
    b = dedup_key("KP", "ASUS RTX 3060 12GB", url, index)
    assert a != b
    # ...and without the classification they collide, which is the bug.
    assert dedup_key("KP", "MSI RTX 3060 12GB", url) == dedup_key("KP", "ASUS RTX 3060 12GB", url)


def test_an_index_listing_is_still_one_listing_across_runs() -> None:
    """Title-keyed identity has to survive the URL's query string changing."""
    index = {"kp.com/graficke/pretraga"}
    first = dedup_key("KP", "MSI RTX 3060 12GB", "https://kp.com/graficke/pretraga?p=1", index)
    later = dedup_key("KP", "MSI RTX 3060 12GB", "https://kp.com/graficke/pretraga?p=2", index)
    assert first == later


def test_a_product_url_still_outranks_a_retitled_listing() -> None:
    """One product page, four title spellings across runs, still one listing."""
    url = "https://konovo.rs/proizvod/hp-elitebook-855-g8"
    assert dedup_key("Konovo", "HP EliteBook 855 G8 (Grade C)", url) == dedup_key(
        "Konovo", "HP EliteBook 855 G8 - Grade C, 16GB RAM", url
    )


# -- comparables and mispricing ------------------------------------------


def _market(*listings: tuple[str, str, float, str]) -> Market:
    return Market(Comparable(k, t, p, c) for k, t, p, c in listings)


GPUS = (
    ("a", "MSI GeForce RTX 3060 12GB VENTUS 2X", 245.0, "EUR"),
    ("b", "RTX 3060 12GB ASUS DUAL", 300.0, "EUR"),
    ("c", "ASUS PH-RTX3060-12G-V2", 310.0, "EUR"),
    ("d", "ASUS Dual-Rtx3060-O12G-V2", 440.0, "EUR"),
    ("e", "RTX 3090 24GB Gigabyte Gaming OC", 1100.0, "EUR"),
    ("f", "RTX 3090 Zotac Trinity 24GB", 1100.0, "EUR"),
)


def test_a_listing_under_its_comparables_scores_above_neutral() -> None:
    market = _market(*GPUS)
    ref = market.reference("new", "Nvidia RTX 3060 12GB", "EUR")
    assert ref is not None
    assert mispricing_score(230.0, ref) > 0.5


def test_a_listing_over_its_comparables_scores_below_neutral() -> None:
    market = _market(*GPUS)
    ref = market.reference("new", "Nvidia RTX 3060 12GB", "EUR")
    assert ref is not None
    assert mispricing_score(600.0, ref) < 0.5


def test_the_going_rate_scores_neutral() -> None:
    market = _market(*GPUS)
    ref = market.reference("new", "RTX 3090 24GB Gigabyte Gaming OC", "EUR")
    assert ref is not None
    assert mispricing_score(ref.price, ref) == 0.5


def test_a_dear_listing_can_outscore_a_cheap_one() -> None:
    """The whole point: 1100 EUR can be the better buy than 440 EUR."""
    market = _market(*GPUS)
    dear = market.reference("x", "RTX 3090 24GB Palit", "EUR")
    cheap = market.reference("y", "RTX 3060 12GB Palit", "EUR")
    assert dear is not None and cheap is not None
    assert mispricing_score(700.0, dear) > mispricing_score(440.0, cheap)


def test_a_terse_model_code_still_finds_its_comparables() -> None:
    """PH-RTX3060-12G-V2 shares nothing with "RTX 3060 12GB" un-split."""
    market = _market(*GPUS)
    ref = market.reference("new", "ASUS PH-RTX3060-12G-V2", "EUR")
    assert ref is not None
    assert "d" in {key for key, _ in ref.peers}


def test_a_verbose_title_still_finds_its_comparables() -> None:
    """Jaccard lost this one: the commentary inflates the union."""
    market = _market(*GPUS)
    ref = market.reference(
        "new",
        "Nvidia RTX 3060 12GB (appears underpriced vs other 3060 12GB listings, EUR 245-440)",
        "EUR",
    )
    assert ref is not None
    assert ref.n >= 1


def test_another_model_class_is_not_a_comparable() -> None:
    """An RTX 3090 says nothing about what an RTX 3060 is worth."""
    market = _market(*GPUS)
    ref = market.reference("new", "MSI RTX 3060 12GB", "EUR")
    assert ref is not None
    assert {"e", "f"}.isdisjoint({key for key, _ in ref.peers})


def test_a_dearer_model_class_arriving_does_not_move_the_score() -> None:
    """Cheapness moves by up to 0.21 here; a valuation must not move at all."""
    listing = ("new", "MSI RTX 3060 12GB", 230.0, "EUR")
    before = _market(*GPUS).reference(*(listing[0], listing[1], listing[3]))
    flooded = _market(
        *GPUS, *[(f"g{i}", "RTX 3090 24GB Gigabyte Gaming OC", 2000.0, "EUR") for i in range(10)]
    )
    after = flooded.reference(listing[0], listing[1], listing[3])
    assert before is not None and after is not None
    assert mispricing_score(230.0, before) == mispricing_score(230.0, after)


def test_prices_in_another_currency_are_never_comparables() -> None:
    market = _market(
        ("a", "MSI GeForce RTX 3060 12GB VENTUS 2X", 245.0, "EUR"),
        ("b", "MSI GeForce RTX 3060 12GB VENTUS 2X", 56519.0, "RSD"),
    )
    ref = market.reference("new", "MSI GeForce RTX 3060 12GB VENTUS 2X", "RSD")
    assert ref is not None
    assert [key for key, _ in ref.peers] == ["b"]


def test_a_listing_is_never_its_own_comparable() -> None:
    market = _market(("a", "MSI GeForce RTX 3060 12GB VENTUS 2X", 245.0, "EUR"))
    assert market.reference("a", "MSI GeForce RTX 3060 12GB VENTUS 2X", "EUR") is None


def test_nothing_comparable_yields_no_reference_rather_than_a_bad_one() -> None:
    market = _market(*GPUS)
    assert market.reference("new", "Bosch dishwasher SMS46KI03E", "EUR") is None


def test_one_peer_is_trusted_less_than_several() -> None:
    """A reference nobody corroborates states half of what it saw."""
    one = Reference(price=100.0, peers=(("a", 0.9),))
    many = Reference(price=100.0, peers=tuple((str(i), 0.9) for i in range(9)))
    assert mispricing_score(50.0, one) < mispricing_score(50.0, many)
    assert mispricing_score(50.0, many) == pytest.approx(0.95)


def test_the_score_stays_inside_its_range() -> None:
    ref = Reference(price=100.0, peers=tuple((str(i), 0.9) for i in range(9)))
    assert mispricing_score(0.01, ref) <= 1.0
    assert mispricing_score(100_000.0, ref) >= 0.0


def test_a_free_reference_price_is_not_a_valuation() -> None:
    assert mispricing_score(10.0, Reference(price=0.0, peers=(("a", 1.0),))) == 0.5


def test_only_the_closest_peers_are_recorded() -> None:
    market = _market(
        *[(f"p{i}", "MSI GeForce RTX 3060 12GB VENTUS 2X", 245.0 + i, "EUR") for i in range(20)]
    )
    ref = market.reference("new", "MSI GeForce RTX 3060 12GB VENTUS 2X", "EUR")
    assert ref is not None
    assert ref.n == MAX_PEERS_RECORDED
    assert list(ref.peers) == sorted(ref.peers, key=lambda p: -p[1])


def test_renaming_a_site_does_not_split_a_listing_in_two() -> None:
    """Scouts renamed Konovo.rs mid-history; the URL is the same listing."""
    url = "https://konovo.rs/proizvod/elitebook-855"
    assert dedup_key("Konovo.rs", "HP EliteBook 855 G8", url) == dedup_key(
        "Konovo.rs (formerly Polovnilaptop.rs)", "HP EliteBook 855 G8", url
    )


def test_without_a_url_the_source_still_separates_two_sites() -> None:
    assert dedup_key("eBay", "ThinkPad T490", None) != dedup_key("Newegg", "ThinkPad T490", None)
