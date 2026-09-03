from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from track.errors import StoreError
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

    assert sorted(store.price_history(assignment.id)) == [100.0, 900.0]


def test_history_ignores_findings_with_no_price(store: Store, assignment) -> None:
    r1 = store.start_run(assignment.id)
    _add(store, assignment.id, r1, "priced", 100.0)
    _add(store, assignment.id, r1, "blocked", None, None)

    assert store.price_history(assignment.id) == [100.0]


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
