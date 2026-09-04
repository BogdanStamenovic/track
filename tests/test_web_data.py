from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import web_support

from track.web.data import (
    WebError,
    connect,
    load_assignments,
    load_listings,
    read_schema,
    search,
    sort_listings,
)


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    return web_support.seed_legacy(tmp_path)


@pytest.fixture
def future_db(tmp_path: Path) -> Path:
    return web_support.seed_future(tmp_path)



def _listings(path: Path):
    conn = connect(path)
    try:
        return load_listings(conn, read_schema(conn), "aa11")
    finally:
        conn.close()


def test_missing_database_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WebError):
        connect(tmp_path / "nope.db")


def test_connection_is_read_only(legacy_db: Path) -> None:
    conn = connect(legacy_db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM findings")
    conn.close()


def test_legacy_schema_reports_every_field_absent(legacy_db: Path) -> None:
    conn = connect(legacy_db)
    schema = read_schema(conn)
    conn.close()
    assert schema.present == {}
    assert "reason" in schema.missing and "model_year" in schema.missing


def test_future_schema_resolves_the_new_columns(future_db: Path) -> None:
    conn = connect(future_db)
    schema = read_schema(conn)
    conn.close()
    # The names track-core actually shipped, resolved through the candidate list.
    assert schema.col("reason") == "rationale"
    assert schema.col("listed_at") == "listing_posted_at"
    assert schema.col("model_year") == "product_year"
    assert schema.col("condition") == "condition"


def test_sightings_collapse_to_one_listing_per_key(legacy_db: Path) -> None:
    listings = _listings(legacy_db)
    assert len(listings) == 3
    widget = next(x for x in listings if x.dedup_key == "k1")
    assert widget.times_seen == 2


def test_a_later_null_does_not_erase_an_observed_price(legacy_db: Path) -> None:
    widget = next(x for x in _listings(legacy_db) if x.dedup_key == "k1")
    assert widget.price == 500.0
    assert widget.currency == "EUR"
    assert widget.price_stale is True
    assert widget.score == 0.9
    assert widget.score_stale is True


def test_absent_values_stay_absent(legacy_db: Path) -> None:
    mystery = next(x for x in _listings(legacy_db) if x.dedup_key == "k2")
    assert mystery.price is None
    assert mystery.score is None
    assert mystery.url is None
    assert mystery.reason is None


def test_first_seen_is_the_earliest_sighting(legacy_db: Path) -> None:
    widget = next(x for x in _listings(legacy_db) if x.dedup_key == "k1")
    assert widget.first_seen_at == "2026-09-01T10:00:00+00:00"
    assert widget.last_seen_at == "2026-09-04T10:00:00+00:00"


def test_dead_is_unknown_when_no_column_says_otherwise(legacy_db: Path) -> None:
    for item in _listings(legacy_db):
        assert item.dead is None, "must not guess a listing is dead"


def test_retirement_is_read_from_listing_status(future_db: Path) -> None:
    by_key = {x.dedup_key: x for x in _listings(future_db)}
    assert by_key["k3"].dead is True
    assert by_key["k3"].retired_reason == "gone"
    assert by_key["k3"].retired_note == "the seller marked it sold"
    assert by_key["k1"].dead is False


def test_a_failed_check_is_not_a_dead_listing(future_db: Path) -> None:
    """The whole point of keeping these apart: a 403 is not a sold item."""
    by_key = {x.dedup_key: x for x in _listings(future_db)}
    unreachable = by_key["k2"]
    assert unreachable.dead is False
    assert unreachable.unverified == "blocked: 403 from the source"
    # and a retired listing is never also reported as merely unverified
    assert by_key["k3"].unverified is None


def test_track_own_counts_win_over_ours(future_db: Path) -> None:
    """listing_status sees runs this query did not fetch, so it is authoritative."""
    widget = next(x for x in _listings(future_db) if x.dedup_key == "k1")
    assert widget.times_seen == 2
    assert widget.first_seen_at == "2026-09-01T10:00:00+00:00"


def test_reason_and_ages_come_through(future_db: Path) -> None:
    widget = next(x for x in _listings(future_db) if x.dedup_key == "k1")
    assert widget.reason and widget.reason.startswith("Cheapest 32GB")
    assert widget.model_year == 2025
    assert str(widget.listed_at).startswith("2026-08-20")


def test_sorts_put_nulls_last(legacy_db: Path) -> None:
    listings = _listings(legacy_db)
    by_score = [x.dedup_key for x in sort_listings(listings, "score")]
    by_price = [x.dedup_key for x in sort_listings(listings, "cheap")]
    by_age = [x.dedup_key for x in sort_listings(listings, "age")]
    assert by_score[-1] == "k2", "an unscored listing is not a zero-scored one"
    assert by_price == ["k3", "k1", "k2"]
    assert by_age[0] == "k1"


def test_search_matches_title_source_and_reason(future_db: Path) -> None:
    listings = _listings(future_db)
    assert {x.dedup_key for x in search(listings, "widget")} == {"k1", "k3"}
    assert {x.dedup_key for x in search(listings, "shopb")} == {"k2"}
    assert {x.dedup_key for x in search(listings, "cheapest 32gb")} == {"k1"}
    assert len(search(listings, "")) == 3


def test_assignments_load(legacy_db: Path) -> None:
    conn = connect(legacy_db)
    assignments = load_assignments(conn)
    conn.close()
    assert [a.id for a in assignments] == ["aa11"]
    assert assignments[0].market == "Serbia"
