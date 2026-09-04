from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from track.errors import StoreError
from track.scoring import dedup_key
from track.store import Store


def _add(store: Store, aid: str, run_id: int, key: str, price: float | None,
         score: float | None = 0.5, source: str = "eBay", is_new: bool = True) -> None:
    store.add_finding(aid, run_id, source, f"t-{key}", price, "USD", None, key, score, is_new)


@pytest.fixture
def assignment(store: Store):
    return store.add_assignment("a laptop", 3600, max_price=500.0)


# -- assignments ---------------------------------------------------------


def test_add_and_get_roundtrip(store: Store) -> None:
    a = store.add_assignment("a laptop", 3600, 500.0, notify_agent="track-dev")
    fetched = store.get_assignment(a.id)

    assert fetched == a
    assert fetched is not None
    assert fetched.status == "active"
    assert fetched.notify_agent == "track-dev"
    assert fetched.runs_count == 0


def test_missing_assignment_is_none(store: Store) -> None:
    assert store.get_assignment("nope") is None


def test_schedule_fields_round_trip(store: Store, assignment) -> None:
    store.set_schedule(assignment.id, "job1", "wake", "2026-01-01T00:00:00+00:00", "job1-resume")
    a = store.get_assignment(assignment.id)

    assert a is not None
    assert (a.job_id, a.backend, a.resume_job_id) == ("job1", "wake", "job1-resume")

    store.clear_schedule(assignment.id)
    a = store.get_assignment(assignment.id)
    assert a is not None
    assert (a.job_id, a.backend, a.resume_job_id) == (None, None, None)


def test_mark_ran_counts_runs(store: Store, assignment) -> None:
    store.mark_ran(assignment.id)
    store.mark_ran(assignment.id)
    a = store.get_assignment(assignment.id)

    assert a is not None
    assert a.runs_count == 2
    assert a.last_run_at is not None


def test_remove_takes_the_history_with_it(store: Store, assignment) -> None:
    run_id = store.start_run(assignment.id)
    _add(store, assignment.id, run_id, "k1", 100.0)
    store.remove_assignment(assignment.id)

    assert store.get_assignment(assignment.id) is None
    assert store.latest_findings(assignment.id) == []
    assert store.list_runs(assignment.id) == []


# -- sources -------------------------------------------------------------


def test_upsert_source_counts_sightings_and_keeps_the_first_url(store: Store, assignment) -> None:
    store.upsert_source(assignment.id, "eBay", "https://ebay.com", "cheap")
    store.upsert_source(assignment.id, "eBay", None, None)
    (source,) = store.list_sources(assignment.id)

    assert source.times_seen == 2
    assert source.url == "https://ebay.com"
    assert source.notes == "cheap"


# -- findings, dedup, history --------------------------------------------


def test_latest_findings_collapses_repeat_sightings(store: Store, assignment) -> None:
    r1 = store.start_run(assignment.id)
    _add(store, assignment.id, r1, "same", 300.0)
    r2 = store.start_run(assignment.id)
    _add(store, assignment.id, r2, "same", 250.0, is_new=False)  # the price dropped

    latest = store.latest_findings(assignment.id)
    assert len(latest) == 1
    assert latest[0].price == 250.0


def test_price_history_is_not_dominated_by_one_re_seen_listing(store: Store, assignment) -> None:
    """Raw rows would count a stale listing once per run it survived."""
    r1 = store.start_run(assignment.id)
    for _ in range(5):
        _add(store, assignment.id, r1, "stale", 900.0)
    _add(store, assignment.id, r1, "bargain", 100.0)

    assert sorted(store.price_history(assignment.id)["USD"]) == [100.0, 900.0]


def test_price_history_never_pools_two_currencies(store: Store, assignment) -> None:
    """3000 RSD against 30 EUR as bare floats makes every dinar look like a steal."""
    r1 = store.start_run(assignment.id)
    store.add_finding(assignment.id, r1, "KP", "t", 3000.0, "RSD", None, "k1", None, True)
    store.add_finding(assignment.id, r1, "eBay", "t", 30.0, "EUR", None, "k2", None, True)

    history = store.price_history(assignment.id)

    assert history == {"RSD": [3000.0], "EUR": [30.0]}


def test_history_ignores_findings_with_no_price(store: Store, assignment) -> None:
    r1 = store.start_run(assignment.id)
    _add(store, assignment.id, r1, "priced", 100.0)
    _add(store, assignment.id, r1, "blocked", None, None)

    assert store.price_history(assignment.id) == {"USD": [100.0]}


def test_has_seen_is_what_makes_a_finding_new(store: Store, assignment) -> None:
    r1 = store.start_run(assignment.id)
    assert not store.has_seen(assignment.id, "k1")
    _add(store, assignment.id, r1, "k1", 100.0)
    assert store.has_seen(assignment.id, "k1")


def test_best_findings_does_not_repeat_one_listing(store: Store, assignment) -> None:
    r1 = store.start_run(assignment.id)
    for _ in range(4):
        _add(store, assignment.id, r1, "winner", 100.0, score=0.99)
    _add(store, assignment.id, r1, "runner-up", 200.0, score=0.6)

    best = store.best_findings(assignment.id, limit=5)
    assert [f.dedup_key for f in best] == ["winner", "runner-up"]


def test_best_findings_ranks_unscored_last(store: Store, assignment) -> None:
    r1 = store.start_run(assignment.id)
    _add(store, assignment.id, r1, "unscored", None, None)
    _add(store, assignment.id, r1, "scored", 100.0, 0.1)

    assert [f.dedup_key for f in store.best_findings(assignment.id)] == ["scored", "unscored"]


# -- runs ----------------------------------------------------------------


def test_run_records_cost(store: Store, assignment) -> None:
    r1 = store.start_run(assignment.id)
    store.finish_run(r1, scout_count=3, findings_count=2, cost_usd=0.25)
    r2 = store.start_run(assignment.id)
    store.finish_run(r2, scout_count=1, findings_count=0, cost_usd=0.10)

    assert store.total_cost(assignment.id) == pytest.approx(0.35)
    runs = store.list_runs(assignment.id)
    assert [r.id for r in runs] == [r2, r1]
    assert runs[1].scout_count == 3


def test_total_cost_of_a_fresh_assignment_is_zero(store: Store, assignment) -> None:
    assert store.total_cost(assignment.id) == 0.0


# -- schema ---------------------------------------------------------------


def test_opening_a_pre_migration_database_adds_the_new_columns(tmp_path: Path) -> None:
    """v0.1.0 databases predate notify_agent/cost_usd; opening one must not fail."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE assignments (id TEXT PRIMARY KEY, text TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            max_price REAL, created_at TEXT NOT NULL, last_run_at TEXT, next_run_at TEXT,
            job_id TEXT, backend TEXT);
        INSERT INTO assignments (id, text, interval_seconds, status, created_at)
            VALUES ('old1', 'a laptop', 3600, 'active', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    with Store(db) as store:
        a = store.get_assignment("old1")
        assert a is not None
        assert a.notify_agent is None
        assert a.runs_count == 0
        store.mark_ran("old1")
        refreshed = store.get_assignment("old1")
        assert refreshed is not None and refreshed.runs_count == 1


def test_reopening_a_migrated_database_is_a_noop(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.add_assignment("a laptop", 3600)
    with Store(db) as store:
        assert len(store.list_assignments()) == 1


def test_undeleteable_path_is_a_store_error(tmp_path: Path) -> None:
    directory = tmp_path / "adir"
    directory.mkdir()
    with pytest.raises(StoreError, match="cannot open database"):
        Store(directory)


def test_index_url_registration_survives_a_reopen(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.add_assignment("laptop", 3600)
        s.register_index_urls("a", {"kp.com/pretraga"})
    with Store(db) as s:
        assert s.index_url_bases("a") == {"kp.com/pretraga"}


def test_repair_splits_listings_that_shared_a_search_url(tmp_path: Path) -> None:
    """The historical rows are recomputed, not dropped: same rows, more listings."""
    db = tmp_path / "t.db"
    url = "https://kp.com/graficke/pretraga?keywords=RTX+3060"
    with Store(db) as s:
        a = s.add_assignment("a gpu", 3600)
        run = s.start_run(a.id)
        for title, price in [("MSI RTX 3060", 245.0), ("ASUS RTX 3060", 399.0)]:
            s.add_finding(
                a.id, run, "KP", title, price, "EUR", url,
                dedup_key("KP", title, url), 0.5, True,
            )
        assert len(s.latest_findings(a.id)) == 1  # the bug, reproduced
        s._conn.execute("DELETE FROM schema_meta WHERE name = 'repair_dedup_keys_v1'")
        s._conn.commit()
    with Store(db) as s:
        rows = s._conn.execute("SELECT count(*) c FROM findings").fetchone()["c"]
        assert rows == 2
        assert len(s.latest_findings(a.id)) == 2
        assert s.index_url_bases(a.id) == {"kp.com/graficke/pretraga"}


def test_repair_leaves_genuine_product_pages_merged(tmp_path: Path) -> None:
    """A product page retitled between runs must stay one listing, not become two."""
    db = tmp_path / "t.db"
    url = "https://konovo.rs/proizvod/elitebook-855"
    with Store(db) as s:
        a = s.add_assignment("a laptop", 3600)
        for title in ["HP EliteBook 855 G8", "HP EliteBook 855 G8 (Grade C)"]:
            run = s.start_run(a.id)
            s.add_finding(
                a.id, run, "Konovo", title, 54500.0, "RSD", url,
                dedup_key("Konovo", title, url), 0.5, True,
            )
        s._conn.execute("DELETE FROM schema_meta WHERE name = 'repair_dedup_keys_v1'")
        s._conn.commit()
    with Store(db) as s:
        assert len(s.latest_findings(a.id)) == 1
        assert s.index_url_bases(a.id) == set()


# -- listing status ------------------------------------------------------


def test_a_listing_records_when_it_was_first_and_last_seen(store: Store) -> None:
    a = store.add_assignment("a laptop", 3600)
    for _ in range(3):
        run = store.start_run(a.id)
        _add(store, a.id, run, "k1", 300.0)

    status = store.listing_status(a.id, "k1")
    assert status is not None
    assert status.times_seen == 3
    assert status.first_seen_at <= status.last_seen_at


def test_backfill_derives_listing_age_from_rows_that_predate_the_table(
    tmp_path: Path,
) -> None:
    """found_at was always there; it just had nowhere to be read from."""
    db = tmp_path / "t.db"
    with Store(db) as s:
        a = s.add_assignment("a laptop", 3600)
        for _ in range(2):
            _add(s, a.id, s.start_run(a.id), "k1", 300.0)
        s._conn.execute("DELETE FROM listing_status")
        s._conn.execute("DELETE FROM schema_meta WHERE name = 'backfill_listing_status_v1'")
        s._conn.commit()
    with Store(db) as s:
        status = s.listing_status(a.id, "k1")
        assert status is not None
        assert status.times_seen == 2


def test_the_listings_view_carries_provenance_and_status(store: Store) -> None:
    a = store.add_assignment("a laptop", 3600)
    run = store.start_run(a.id)
    store.add_finding(
        a.id, run, "KP", "ThinkPad T490", 230.0, "EUR", "https://kp/1", "k1", 0.8, True,
        rationale="cheapest T490 on the site", condition="used", product_year=2019,
    )

    rows = store._conn.execute("SELECT * FROM listings_current").fetchall()

    assert len(rows) == 1
    assert rows[0]["rationale"] == "cheapest T490 on the site"
    assert rows[0]["product_year"] == 2019
    assert rows[0]["times_seen"] == 1
    assert rows[0]["retired_at"] is None


def test_the_view_shows_one_row_per_listing_not_per_sighting(store: Store) -> None:
    a = store.add_assignment("a laptop", 3600)
    for price in (300.0, 280.0):
        _add(store, a.id, store.start_run(a.id), "k1", price)

    rows = store._conn.execute("SELECT price FROM listings_current").fetchall()

    assert [r["price"] for r in rows] == [280.0]
