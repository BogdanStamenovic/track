"""Engine tests: one research cycle, end to end, with the scouts faked out."""

from __future__ import annotations

import pytest

from track.engine import REDISCOVER_EVERY, ensure_sources, run_assignment
from track.errors import ScoutError
from track.scouts import ScoutFinding, ScoutResult
from track.store import Store


@pytest.fixture(autouse=True)
def no_posting(monkeypatch):
    """Nothing in this module may reach Discord or a scheduler."""
    monkeypatch.setattr("track.engine.post_summary", lambda *a, **k: None)
    monkeypatch.setattr("track.engine.scheduler.schedule", _unexpected("schedule"))


def _unexpected(what: str):
    def boom(*a, **k):
        raise AssertionError(f"{what} should not have been called")

    return boom


def _fake_scouts(monkeypatch, findings, *, cost=0.0, blocked=0):
    monkeypatch.setattr(
        "track.engine.scouts.run_scouts",
        lambda *a, **k: ScoutResult(findings=list(findings), cost_usd=cost, blocked=blocked),
    )


def _fake_discovery(monkeypatch, sources, *, cost=0.0):
    monkeypatch.setattr(
        "track.engine.scouts.discover_sources", lambda *a, **k: (list(sources), cost)
    )


def _sf(title: str, price: float | None, source: str = "eBay", url: str | None = None):
    return ScoutFinding(source, title, price, "USD", url or f"https://x/{title}")


# -- source discovery ----------------------------------------------------


def test_discovery_runs_when_an_assignment_has_no_sources(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    _fake_discovery(monkeypatch, [{"source": "eBay", "url": "https://ebay.com", "notes": "cheap"}])

    names, cost = ensure_sources(store, a)

    assert names == ["eBay"]
    assert [s.name for s in store.list_sources(a.id)] == ["eBay"]
    assert cost == 0.0


def test_discovery_is_skipped_when_sources_are_already_known(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")
    monkeypatch.setattr("track.engine.scouts.discover_sources", _unexpected("discover_sources"))

    assert ensure_sources(store, a)[0] == ["eBay"]


def test_discovery_repeats_periodically_so_the_source_list_cannot_ossify(
    store: Store, monkeypatch
) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")
    for _ in range(REDISCOVER_EVERY):
        store.mark_ran(a.id)
    refreshed = store.get_assignment(a.id)
    assert refreshed is not None
    _fake_discovery(monkeypatch, [{"source": "Craigslist"}])

    names, _cost = ensure_sources(store, refreshed)

    assert names == ["eBay", "Craigslist"]


def test_failed_discovery_warns_and_keeps_what_is_known(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)

    def boom(*args, **kwargs):
        raise ScoutError("claude CLI not found on PATH")

    monkeypatch.setattr("track.engine.scouts.discover_sources", boom)
    warnings: list[str] = []

    names, _cost = ensure_sources(store, a, warn=warnings.append)

    assert names == []
    assert any("source discovery failed" in w for w in warnings)


# -- a run ---------------------------------------------------------------


def test_a_run_stores_scores_and_summarises(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [_sf("ThinkPad", 300.0)], cost=0.12)

    findings, summary = run_assignment(store, a)

    assert len(findings) == 1
    assert findings[0].is_new is True
    assert findings[0].score == 0.5  # no history yet
    assert "ThinkPad" in summary
    assert store.total_cost(a.id) == pytest.approx(0.12)
    refreshed = store.get_assignment(a.id)
    assert refreshed is not None and refreshed.runs_count == 1


def test_the_second_sighting_of_a_listing_is_not_new(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [_sf("ThinkPad", 300.0)])

    run_assignment(store, a)
    second, _summary = run_assignment(store, store.get_assignment(a.id) or a)

    assert second[0].is_new is False


def test_scores_do_not_depend_on_which_scout_returned_first(tmp_path, monkeypatch) -> None:
    """History is frozen at run start; extending it mid-run made scores order-dependent."""
    batch = [_sf("a", 100.0), _sf("b", 200.0), _sf("c", 300.0)]

    def scores_for(label, order):
        with Store(tmp_path / f"{label}.db") as s:
            assignment = s.add_assignment("a laptop", 3600)
            s.upsert_source(assignment.id, "eBay")
            # Seed history first, otherwise every score is the neutral 0.5 and
            # the comparison below would hold even for an order-dependent scorer.
            _fake_scouts(monkeypatch, [_sf("seed", 250.0)])
            run_assignment(s, assignment)
            _fake_scouts(monkeypatch, order)
            found, _summary = run_assignment(s, s.get_assignment(assignment.id) or assignment)
            return {f.title: f.score for f in found}

    forwards = scores_for("fwd", batch)
    backwards = scores_for("rev", list(reversed(batch)))

    assert forwards == backwards
    assert forwards["a"] == 1.0, "cheaper than the seed"
    assert forwards["c"] == 0.0, "dearer than the seed"


def test_a_price_drop_is_scored_against_the_history_that_existed(
    store: Store, monkeypatch
) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")

    _fake_scouts(monkeypatch, [_sf("expensive", 900.0)])
    run_assignment(store, a)

    _fake_scouts(monkeypatch, [_sf("bargain", 100.0)])
    second, _summary = run_assignment(store, store.get_assignment(a.id) or a)

    assert second[0].score == 1.0


def test_an_earlier_finding_is_never_rescored(store: Store, monkeypatch) -> None:
    """Append-only: a later cheaper listing must not demote yesterday's row."""
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")

    _fake_scouts(monkeypatch, [_sf("first", 500.0)])
    (first,), _summary = run_assignment(store, a)
    original_score = first.score

    _fake_scouts(monkeypatch, [_sf("cheaper", 10.0)])
    run_assignment(store, store.get_assignment(a.id) or a)

    still_there = next(f for f in store.latest_findings(a.id) if f.title == "first")
    assert still_there.score == original_score


def test_an_unpriced_listing_is_stored_unscored_not_dropped(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [_sf("blocked", None)], blocked=1)

    warnings: list[str] = []
    findings, _summary = run_assignment(store, a, warn=warnings.append)

    assert findings[0].price is None
    assert findings[0].score is None
    assert any("without a price" in w for w in warnings)


def test_scouted_sources_are_learned(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [_sf("t", 10.0, source="Marktplaats")])

    run_assignment(store, a)

    assert "Marktplaats" in {s.name for s in store.list_sources(a.id)}


def test_no_sources_yields_an_honest_empty_run(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    _fake_discovery(monkeypatch, [])
    _fake_scouts(monkeypatch, [])

    findings, summary = run_assignment(store, a)

    assert findings == []
    assert "Nothing new this run." in summary


# -- posting and re-arming ------------------------------------------------


def test_the_summary_goes_to_the_assignments_own_agent(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600, notify_agent="track-dev")
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [_sf("t", 10.0)])
    seen: dict = {}
    monkeypatch.setattr(
        "track.engine.post_summary", lambda summary, agent=None: seen.update(agent=agent)
    )

    run_assignment(store, a, post=True)

    assert seen["agent"] == "track-dev"


def test_a_failed_post_warns_but_the_findings_are_still_kept(
    store: Store, monkeypatch
) -> None:
    from track.errors import ReportError

    a = store.add_assignment("a laptop", 3600)
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [_sf("t", 10.0)])
    monkeypatch.setattr(
        "track.engine.post_summary", _raiser(ReportError("hotline-say exited 1"))
    )
    warnings: list[str] = []

    findings, _summary = run_assignment(store, a, post=True, warn=warnings.append)

    assert len(findings) == 1
    assert any("could not post summary" in w for w in warnings)


def test_a_wake_backed_assignment_rearms_itself(store: Store, monkeypatch) -> None:
    from track.scheduler import ScheduleResult

    a = store.add_assignment("a laptop", 3600, wake_backend="rtcwake")
    store.set_schedule(a.id, "job-1", "wake", "2026-01-01T00:00:00+00:00")
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [])
    calls: list[dict] = []
    monkeypatch.setattr(
        "track.engine.scheduler.schedule",
        lambda *args, **kw: calls.append(kw)
        or ScheduleResult("job-2", "wake", "2026-01-01T01:00:00+00:00", "job-2-resume"),
    )

    run_assignment(store, store.get_assignment(a.id) or a)

    assert calls[0]["wake_backend"] == "rtcwake"
    refreshed = store.get_assignment(a.id)
    assert refreshed is not None
    assert (refreshed.job_id, refreshed.resume_job_id) == ("job-2", "job-2-resume")


def test_a_systemd_backed_assignment_does_not_rearm(store: Store, monkeypatch) -> None:
    """The timer already recurs; re-arming it would be a second schedule."""
    a = store.add_assignment("a laptop", 3600)
    store.set_schedule(a.id, "track-a1.timer", "systemd-timer", "2026-01-01T00:00:00+00:00")
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [])

    run_assignment(store, store.get_assignment(a.id) or a)  # scheduler.schedule would raise


def test_a_paused_assignment_does_not_rearm(store: Store, monkeypatch) -> None:
    a = store.add_assignment("a laptop", 3600)
    store.set_schedule(a.id, "job-1", "wake", "2026-01-01T00:00:00+00:00")
    store.set_status(a.id, "paused")
    store.upsert_source(a.id, "eBay")
    _fake_scouts(monkeypatch, [])

    run_assignment(store, store.get_assignment(a.id) or a)


def _raiser(exc: Exception):
    def boom(*a, **k):
        raise exc

    return boom
