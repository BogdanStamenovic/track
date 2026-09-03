from __future__ import annotations

from track.engine import ensure_sources, run_assignment
from track.errors import TrackError
from track.scouts import ScoutFinding
from track.store import Store


def test_ensure_sources_uses_existing_when_present(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)
    store.upsert_source(a.id, "eBay", "https://ebay.com")

    def fail_discover(*args, **kwargs):
        raise AssertionError("should not re-discover when sources already exist")

    monkeypatch.setattr("track.engine.scouts.discover_sources", fail_discover)
    names = ensure_sources(store, a)
    assert names == ["eBay"]


def test_ensure_sources_discovers_when_empty(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)

    monkeypatch.setattr(
        "track.engine.scouts.discover_sources",
        lambda text, **kw: [{"source": "eBay", "url": "https://ebay.com", "notes": "cheap"}],
    )
    names = ensure_sources(store, a)
    assert names == ["eBay"]
    assert store.list_sources(a.id)[0].name == "eBay"


def test_ensure_sources_warns_and_returns_empty_on_failure(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)

    def boom(*args, **kwargs):
        raise TrackError("network down")

    monkeypatch.setattr("track.engine.scouts.discover_sources", boom)
    warnings = []
    names = ensure_sources(store, a, warn=warnings.append)
    assert names == []
    assert any("network down" in w for w in warnings)


def test_run_assignment_scores_against_history(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)
    store.upsert_source(a.id, "eBay")

    monkeypatch.setattr(
        "track.engine.scouts.run_scouts",
        lambda text, sources, **kw: [
            ScoutFinding(source="eBay", title="ThinkPad", price=100.0, currency="USD", url="https://x/1")
        ],
    )
    monkeypatch.setattr("track.engine.post_summary", lambda summary, **kw: None)

    findings, summary = run_assignment(store, a, post=True)
    assert len(findings) == 1
    assert findings[0].score == 0.5  # no prior history yet
    assert findings[0].is_new is True
    assert "ThinkPad" in summary


def test_run_assignment_marks_repeat_finding_not_new(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)
    store.upsert_source(a.id, "eBay")

    same_finding = ScoutFinding(
        source="eBay", title="ThinkPad", price=100.0, currency="USD", url="https://x/1"
    )
    monkeypatch.setattr("track.engine.scouts.run_scouts", lambda text, sources, **kw: [same_finding])
    monkeypatch.setattr("track.engine.post_summary", lambda summary, **kw: None)

    first, _ = run_assignment(store, a, post=True)
    second, _ = run_assignment(store, a, post=True)
    assert first[0].is_new is True
    assert second[0].is_new is False


def test_run_assignment_report_failure_does_not_raise(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)
    store.upsert_source(a.id, "eBay")
    monkeypatch.setattr("track.engine.scouts.run_scouts", lambda text, sources, **kw: [])

    def boom(summary, **kw):
        raise TrackError("discord down")

    monkeypatch.setattr("track.engine.post_summary", boom)
    warnings = []
    run_assignment(store, a, warn=warnings.append, post=True)
    assert any("discord down" in w for w in warnings)


def test_run_assignment_rearms_wake_backend(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)
    store.set_schedule(a.id, "task-1", "wake", "then")
    a = store.get_assignment(a.id)
    assert a is not None
    store.upsert_source(a.id, "eBay")

    monkeypatch.setattr("track.engine.scouts.run_scouts", lambda text, sources, **kw: [])
    monkeypatch.setattr("track.engine.post_summary", lambda summary, **kw: None)

    calls = []

    def fake_schedule(assignment_id, interval_seconds, cmd, **kw):
        calls.append((assignment_id, interval_seconds))
        from track.scheduler import ScheduleResult

        return ScheduleResult(job_id="task-2", backend="wake", next_run_at="later")

    monkeypatch.setattr("track.engine.scheduler.schedule", fake_schedule)
    run_assignment(store, a, post=True)

    assert calls == [(a.id, 3600)]
    updated = store.get_assignment(a.id)
    assert updated is not None
    assert updated.job_id == "task-2"


def test_run_assignment_does_not_rearm_systemd_timer(store: Store, monkeypatch) -> None:
    a = store.add_assignment("laptops", 3600)
    store.set_schedule(a.id, "track-x.timer", "systemd-timer", "then")
    a = store.get_assignment(a.id)
    assert a is not None
    store.upsert_source(a.id, "eBay")

    monkeypatch.setattr("track.engine.scouts.run_scouts", lambda text, sources, **kw: [])
    monkeypatch.setattr("track.engine.post_summary", lambda summary, **kw: None)

    def fail_schedule(*args, **kwargs):
        raise AssertionError("systemd-timer assignments should not re-arm")

    monkeypatch.setattr("track.engine.scheduler.schedule", fail_schedule)
    run_assignment(store, a, post=True)
