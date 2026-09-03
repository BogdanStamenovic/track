"""CLI tests: exit codes, stdout/stderr discipline, and argument plumbing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from track.cli import main
from track.scheduler import ScheduleResult
from track.store import Store


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "track.db"


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """Nothing in this module may schedule, scout, or post."""
    monkeypatch.setattr(
        "track.cli.schedule_wakeup",
        lambda *a, **k: ScheduleResult("job-1", "wake", "2026-01-01T00:00:00+00:00"),
    )
    monkeypatch.setattr("track.cli.cancel_schedule", lambda *a, **k: None)
    monkeypatch.delenv("TRACK_HOTLINE_AGENT", raising=False)
    monkeypatch.delenv("TRACK_HOTLINE_CHANNEL", raising=False)


def _add(db: Path, *extra: str) -> str:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--db", str(db), "add", "a laptop", *extra]) == 0
    return out.getvalue().strip()


# -- usage ---------------------------------------------------------------


def test_no_subcommand_is_a_usage_error(capsys) -> None:
    assert main([]) == 2
    assert "error" in capsys.readouterr().err


def test_bad_interval_is_a_usage_error(capsys, db: Path) -> None:
    assert main(["--db", str(db), "add", "x", "--interval", "soon"]) == 2
    assert "invalid --interval" in capsys.readouterr().err


def test_zero_interval_is_rejected(db: Path) -> None:
    assert main(["--db", str(db), "add", "x", "--interval", "0h"]) == 2


def test_wol_without_a_target_is_a_usage_error(capsys, db: Path) -> None:
    rc = main(["--db", str(db), "add", "x", "--wake-backend", "wol"])
    assert rc == 2
    assert "--wake-target" in capsys.readouterr().err


def test_unknown_assignment_is_a_failure_not_a_usage_error(capsys, db: Path) -> None:
    assert main(["--db", str(db), "show", "nope"]) == 1
    assert "no such assignment" in capsys.readouterr().err


# -- add -----------------------------------------------------------------


def test_add_prints_only_the_id_on_stdout(capsys, db: Path) -> None:
    assert main(["--db", str(db), "add", "a laptop"]) == 0
    captured = capsys.readouterr()

    assert len(captured.out.strip()) == 8
    assert "tracking" in captured.err  # progress goes to stderr


def test_add_stores_the_options(db: Path) -> None:
    assignment_id = _add(
        db, "--interval", "2h", "--max-price", "400", "--notify", "track-dev",
        "--wake-backend", "wol", "--wake-target", "aa:bb:cc:dd:ee:ff",
        "--wake-on", "archserver",
    )
    with Store(db) as store:
        a = store.get_assignment(assignment_id)

    assert a is not None
    assert a.interval_seconds == 7200
    assert a.max_price == 400.0
    assert a.notify_agent == "track-dev"
    assert (a.wake_backend, a.wake_target) == ("wol", "aa:bb:cc:dd:ee:ff")
    assert a.wake_on == "archserver"


def test_wake_options_reach_the_scheduler(db: Path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        "track.cli.schedule_wakeup",
        lambda *a, **kw: seen.update(kw)
        or ScheduleResult("job-1", "wake", "2026-01-01T00:00:00+00:00"),
    )
    _add(db, "--wake-backend", "wol", "--wake-target", "aa:bb", "--wake-on", "archserver",
         "--notify", "x")

    assert seen["wake_backend"] == "wol"
    assert seen["target"] == "aa:bb"
    assert seen["run_on"] == "archserver"


@pytest.mark.parametrize(
    ("text", "seconds"), [("90s", 90), ("30m", 1800), ("6h", 21600), ("2d", 172800), ("45", 45)]
)
def test_interval_units(db: Path, text: str, seconds: int) -> None:
    assignment_id = _add(db, "--interval", text)
    with Store(db) as store:
        a = store.get_assignment(assignment_id)
    assert a is not None and a.interval_seconds == seconds


def test_scheduling_a_run_with_no_notify_target_warns(capsys, db: Path) -> None:
    """That run would find things and have nowhere to report them."""
    main(["--db", str(db), "add", "a laptop"])
    assert "no --notify agent" in capsys.readouterr().err


def test_no_schedule_skips_both_the_warning_and_the_scheduler(capsys, db: Path, monkeypatch) -> None:
    monkeypatch.setattr("track.cli.schedule_wakeup", _boom)
    assert main(["--db", str(db), "add", "a laptop", "--no-schedule"]) == 0
    assert "no --notify agent" not in capsys.readouterr().err


def test_a_scheduler_failure_does_not_lose_the_assignment(capsys, db: Path, monkeypatch) -> None:
    from track.errors import SchedulerError

    monkeypatch.setattr("track.cli.schedule_wakeup", _raiser(SchedulerError("wake add failed")))
    rc = main(["--db", str(db), "add", "a laptop", "--notify", "x"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "could not schedule" in captured.err
    assert len(captured.out.strip()) == 8


# -- list / show / sources -----------------------------------------------


def test_list_is_empty_on_stdout_when_there_is_nothing(capsys, db: Path) -> None:
    assert main(["--db", str(db), "list"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no assignments" in captured.err


def test_list_prints_one_row_per_assignment(capsys, db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    capsys.readouterr()
    main(["--db", str(db), "list"])

    out = capsys.readouterr().out
    assert assignment_id in out
    assert "a laptop" in out
    assert "active" in out


def test_list_json_is_parseable(capsys, db: Path) -> None:
    _add(db, "--notify", "x")
    capsys.readouterr()
    main(["--db", str(db), "list", "--json"])

    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["text"] == "a laptop"


def test_show_reports_schedule_sources_and_spend(capsys, db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    with Store(db) as store:
        store.upsert_source(assignment_id, "eBay", "https://ebay.com")
        run_id = store.start_run(assignment_id)
        store.add_finding(
            assignment_id, run_id, "eBay", "ThinkPad", 300.0, "USD",
            "https://ebay.com/1", "k1", 0.9, True,
        )
        store.finish_run(run_id, 1, 1, 0.2)
    capsys.readouterr()

    main(["--db", str(db), "show", assignment_id])
    out = capsys.readouterr().out

    assert "eBay" in out
    assert "ThinkPad" in out
    assert "300.00 USD" in out
    assert "$0.200 spent" in out


def test_sources_reports_statistics(capsys, db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    with Store(db) as store:
        run_id = store.start_run(assignment_id)
        for key, price in [("k1", 100.0), ("k2", 300.0), ("k3", None)]:
            store.add_finding(
                assignment_id, run_id, "eBay", f"t{key}", price, "USD", None, key, 0.5, True
            )
    capsys.readouterr()

    main(["--db", str(db), "sources", assignment_id])
    out = capsys.readouterr().out

    assert "eBay (USD): 3 listings, 2 priced (67%)" in out
    assert "from 100.00 USD" in out
    assert "median 200.00 USD" in out


def test_sources_with_no_findings_says_so_on_stderr(capsys, db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    capsys.readouterr()
    assert main(["--db", str(db), "sources", assignment_id]) == 0
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "run the assignment first" in captured.err


def test_sources_json_is_parseable(capsys, db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    with Store(db) as store:
        run_id = store.start_run(assignment_id)
        store.add_finding(
            assignment_id, run_id, "eBay", "t", 100.0, "USD", None, "k1", 0.5, True
        )
    capsys.readouterr()

    main(["--db", str(db), "sources", assignment_id, "--json"])
    assert json.loads(capsys.readouterr().out)[0]["cheapest"] == 100.0


# -- run -----------------------------------------------------------------


def test_run_prints_the_summary_to_stdout(capsys, db: Path, monkeypatch) -> None:
    assignment_id = _add(db, "--notify", "x")
    monkeypatch.setattr("track.cli.run_assignment", lambda *a, **k: ([], "SUMMARY"))
    capsys.readouterr()

    assert main(["--db", str(db), "run", assignment_id, "--no-post"]) == 0
    assert capsys.readouterr().out.strip() == "SUMMARY"


def test_run_passes_the_no_post_flag_through(db: Path, monkeypatch) -> None:
    assignment_id = _add(db, "--notify", "x")
    seen: dict = {}
    monkeypatch.setattr(
        "track.cli.run_assignment",
        lambda store, a, warn=None, post=True: seen.update(post=post) or ([], ""),
    )

    main(["--db", str(db), "run", assignment_id, "--no-post"])
    assert seen["post"] is False
    main(["--db", str(db), "run", assignment_id])
    assert seen["post"] is True


def test_a_paused_assignment_is_not_run_by_accident(capsys, db: Path, monkeypatch) -> None:
    assignment_id = _add(db, "--notify", "x")
    main(["--db", str(db), "pause", assignment_id])
    monkeypatch.setattr("track.cli.run_assignment", _boom)
    capsys.readouterr()

    assert main(["--db", str(db), "run", assignment_id]) == 1
    assert "is paused" in capsys.readouterr().err


def test_force_runs_a_paused_assignment(db: Path, monkeypatch) -> None:
    assignment_id = _add(db, "--notify", "x")
    main(["--db", str(db), "pause", assignment_id])
    monkeypatch.setattr("track.cli.run_assignment", lambda *a, **k: ([], "ok"))

    assert main(["--db", str(db), "run", assignment_id, "--force"]) == 0


# -- lifecycle -----------------------------------------------------------


def test_pause_cancels_the_schedule_and_keeps_the_history(db: Path, monkeypatch) -> None:
    cancelled: list[tuple] = []
    monkeypatch.setattr(
        "track.cli.cancel_schedule",
        lambda job, backend, **kw: cancelled.append((job, backend, kw.get("resume_job_id"))),
    )
    assignment_id = _add(db, "--notify", "x")

    assert main(["--db", str(db), "pause", assignment_id]) == 0
    with Store(db) as store:
        a = store.get_assignment(assignment_id)

    assert cancelled == [("job-1", "wake", None)]
    assert a is not None
    assert a.status == "paused"
    assert a.job_id is None


def test_resume_reschedules(db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    main(["--db", str(db), "pause", assignment_id])

    assert main(["--db", str(db), "resume", assignment_id]) == 0
    with Store(db) as store:
        a = store.get_assignment(assignment_id)

    assert a is not None
    assert a.status == "active"
    assert a.job_id == "job-1"


def test_remove_cancels_and_deletes(db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")

    assert main(["--db", str(db), "remove", assignment_id]) == 0
    with Store(db) as store:
        assert store.get_assignment(assignment_id) is None


def test_removing_an_unknown_assignment_fails(db: Path) -> None:
    assert main(["--db", str(db), "remove", "nope"]) == 1


def test_quiet_silences_stderr_but_not_stdout(capsys, db: Path) -> None:
    assert main(["--db", str(db), "-q", "add", "a laptop", "--no-schedule"]) == 0
    captured = capsys.readouterr()

    assert captured.err == ""
    assert len(captured.out.strip()) == 8


def _boom(*a, **k):
    raise AssertionError("should not have been called")


def _raiser(exc: Exception):
    def boom(*a, **k):
        raise exc

    return boom


def test_market_is_stored_and_defaults_from_the_environment(db: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRACK_MARKET", "Serbia")
    assignment_id = _add(db, "--notify", "x")
    with Store(db) as store:
        a = store.get_assignment(assignment_id)

    assert a is not None and a.market == "Serbia"


def test_an_explicit_market_beats_the_environment(db: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRACK_MARKET", "Serbia")
    assignment_id = _add(db, "--market", "Croatia", "--notify", "x")
    with Store(db) as store:
        a = store.get_assignment(assignment_id)

    assert a is not None and a.market == "Croatia"


def test_adding_without_a_market_warns_that_results_will_be_foreign(
    capsys, db: Path, monkeypatch
) -> None:
    monkeypatch.delenv("TRACK_MARKET", raising=False)
    main(["--db", str(db), "add", "a laptop", "--notify", "x"])

    assert "no --market set" in capsys.readouterr().err
