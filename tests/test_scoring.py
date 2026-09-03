from __future__ import annotations

from track.scoring import dedup_key, underpriced_score


def test_dedup_key_stable_across_query_params() -> None:
    a = dedup_key("eBay", "ThinkPad", "https://ebay.com/item/123?ref=abc")
    b = dedup_key("eBay", "ThinkPad", "https://ebay.com/item/123?ref=xyz")
    assert a == b


def test_dedup_key_case_insensitive_source() -> None:
    a = dedup_key("eBay", "x", "https://ebay.com/item/1")
    b = dedup_key("EBAY", "x", "https://ebay.com/item/1")
    assert a == b


def test_dedup_key_falls_back_to_title_without_url() -> None:
    a = dedup_key("Craigslist", "Nice Laptop", None)
    b = dedup_key("Craigslist", "  nice   laptop  ", None)
    assert a == b


def test_dedup_key_differs_for_different_urls() -> None:
    a = dedup_key("eBay", "x", "https://ebay.com/item/1")
    b = dedup_key("eBay", "x", "https://ebay.com/item/2")
    assert a != b


def test_underpriced_score_no_history_is_neutral() -> None:
    assert underpriced_score(100.0, []) == 0.5


def test_underpriced_score_cheapest_ever_is_one() -> None:
    assert underpriced_score(10.0, [50.0, 60.0, 70.0]) == 1.0


def test_underpriced_score_priciest_ever_is_zero() -> None:
    assert underpriced_score(100.0, [50.0, 60.0, 70.0]) == 0.0


def test_underpriced_score_middle_of_pack() -> None:
    assert underpriced_score(50.0, [10.0, 50.0, 100.0]) == 2 / 3
