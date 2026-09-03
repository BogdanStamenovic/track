from __future__ import annotations

from track.store import Store


def test_add_and_get_assignment(store: Store) -> None:
    a = store.add_assignment("a cheap laptop", 3600)
    assert a.text == "a cheap laptop"
    assert a.status == "active"
    fetched = store.get_assignment(a.id)
    assert fetched == a


def test_get_missing_assignment_returns_none(store: Store) -> None:
    assert store.get_assignment("nope") is None


def test_list_assignments_ordered_by_creation(store: Store) -> None:
    first = store.add_assignment("first", 3600)
    second = store.add_assignment("second", 3600)
    assignments = store.list_assignments()
    assert [a.id for a in assignments] == [first.id, second.id]


def test_set_and_clear_schedule(store: Store) -> None:
    a = store.add_assignment("x", 3600)
    store.set_schedule(a.id, "job-1", "wake", "2026-01-01T00:00:00+00:00")
    fetched = store.get_assignment(a.id)
    assert fetched is not None
    assert fetched.job_id == "job-1"
    assert fetched.backend == "wake"

    store.clear_schedule(a.id)
    fetched = store.get_assignment(a.id)
    assert fetched is not None
    assert fetched.job_id is None
    assert fetched.backend is None


def test_set_status_and_remove(store: Store) -> None:
    a = store.add_assignment("x", 3600)
    store.set_status(a.id, "paused")
    assert store.get_assignment(a.id).status == "paused"  # type: ignore[union-attr]
    store.remove_assignment(a.id)
    assert store.get_assignment(a.id) is None


def test_upsert_source_increments_times_seen(store: Store) -> None:
    a = store.add_assignment("x", 3600)
    store.upsert_source(a.id, "eBay", "https://ebay.com", "cheap")
    store.upsert_source(a.id, "eBay", "https://ebay.com/2")
    sources = store.list_sources(a.id)
    assert len(sources) == 1
    assert sources[0].times_seen == 2
    assert sources[0].url == "https://ebay.com/2"
    assert sources[0].notes == "cheap"  # preserved via COALESCE


def test_run_and_finding_lifecycle(store: Store) -> None:
    a = store.add_assignment("x", 3600)
    run_id = store.start_run(a.id)
    assert store.price_history(a.id) == []
    assert store.has_seen(a.id, "key1") is False

    finding = store.add_finding(
        a.id, run_id, "eBay", "ThinkPad", 300.0, "USD", "https://x", "key1", 0.9, True
    )
    assert finding.price == 300.0
    assert store.has_seen(a.id, "key1") is True
    assert store.price_history(a.id) == [300.0]

    store.finish_run(run_id, scout_count=1, findings_count=1)
    run_findings = store.run_findings(run_id)
    assert len(run_findings) == 1

    best = store.best_findings(a.id, limit=5)
    assert best[0].id == finding.id


def test_remove_assignment_cascades(store: Store) -> None:
    a = store.add_assignment("x", 3600)
    store.upsert_source(a.id, "eBay")
    run_id = store.start_run(a.id)
    store.add_finding(a.id, run_id, "eBay", "t", 1.0, "USD", None, "k", 0.5, True)

    store.remove_assignment(a.id)

    assert store.get_assignment(a.id) is None
    assert store.list_sources(a.id) == []
    assert store.price_history(a.id) == []
