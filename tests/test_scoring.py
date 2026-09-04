from __future__ import annotations

from track.models import Finding
from track.scoring import dedup_key, index_url_bases, source_stats, underpriced_score


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
