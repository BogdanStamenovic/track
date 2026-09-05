"""CLI tests: exit codes, stdout/stderr discipline, and argument plumbing."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from track.cli import SUBCOMMANDS, _build_parser, _split_web_args, main
from track.engine import RunOutcome
from track.errors import ScoutError, TrackError
from track.scheduler import ScheduleResult
from track.scouts import ScheduleAdvice
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
    # Slot scheduling does not go through `schedule_wakeup`; it goes through
    # slots.arm, which probes and calls the real wake. Faking only the first
    # left `track add --at` shelling out from a unit test.
    monkeypatch.setattr("track.cli.slots.arm", fake_arm)
    monkeypatch.delenv("TRACK_HOTLINE_AGENT", raising=False)
    monkeypatch.delenv("TRACK_HOTLINE_CHANNEL", raising=False)


def fake_arm(store: Store, check_at: str, run_cmd: list[str], **kwargs: object):
    """What slots.arm does, minus the subprocess.

    It has to write the schedule back onto every member, because that is how
    a member knows which shared task to cancel when it is the last one out.
    """
    members = store.slot_members(check_at)
    if not members:
        return None
    result = ScheduleResult(
        f"track-slot-{check_at.replace(':', '')}", "wake", "2026-01-01T00:00:00+00:00"
    )
    for member in members:
        store.set_schedule(member.id, result.job_id, result.backend, result.next_run_at)
    return result


def _add(db: Path, *extra: str) -> str:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        # --no-advise by default: these tests are about everything except
        # the advisor, and it is the one code path that would shell out.
        assert main(["--db", str(db), "add", "a laptop", "--no-advise", *extra]) == 0
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
    assert main(["--db", str(db), "add", "a laptop", "--no-advise"]) == 0
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
    main(["--db", str(db), "add", "a laptop", "--no-advise"])
    assert "no --notify agent" in capsys.readouterr().err


def test_no_schedule_skips_both_the_warning_and_the_scheduler(capsys, db: Path, monkeypatch) -> None:
    monkeypatch.setattr("track.cli.schedule_wakeup", _boom)
    assert main(["--db", str(db), "add", "a laptop", "--no-schedule"]) == 0
    assert "no --notify agent" not in capsys.readouterr().err


def test_a_scheduler_failure_does_not_lose_the_assignment(capsys, db: Path, monkeypatch) -> None:
    from track.errors import SchedulerError

    monkeypatch.setattr("track.cli.schedule_wakeup", _raiser(SchedulerError("wake add failed")))
    rc = main(["--db", str(db), "add", "a laptop", "--no-advise", "--notify", "x"])
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


def test_show_reports_schedule_sources_and_usage(capsys, db: Path) -> None:
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
    assert "~$0.20 of model usage" in out


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
    monkeypatch.setattr(
        "track.cli.run_assignment",
        lambda *a, **k: RunOutcome([], "SUMMARY", posted=True, usable=1, scout_failures=0),
    )
    capsys.readouterr()

    assert main(["--db", str(db), "run", assignment_id, "--no-post"]) == 0
    assert capsys.readouterr().out.strip() == "SUMMARY"


def test_run_passes_the_no_post_flag_through(db: Path, monkeypatch) -> None:
    assignment_id = _add(db, "--notify", "x")
    seen: dict = {}
    monkeypatch.setattr(
        "track.cli.run_assignment",
        lambda store, a, warn=None, post=True: seen.update(post=post)
        or RunOutcome([], "", posted=True, usable=1, scout_failures=0),
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
    monkeypatch.setattr(
        "track.cli.run_assignment",
        lambda *a, **k: RunOutcome([], "ok", posted=True, usable=1, scout_failures=0),
    )

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
    main(["--db", str(db), "add", "a laptop", "--no-advise", "--notify", "x"])

    assert "no --market set" in capsys.readouterr().err


# -- exit codes, which are the only signal a 08:00 run can send ------------


def _outcome(monkeypatch, **kw):
    defaults = {"findings": [], "summary": "S", "posted": True, "usable": 1,
                "scout_failures": 0}
    defaults.update(kw)
    monkeypatch.setattr("track.cli.run_assignment", lambda *a, **k: RunOutcome(**defaults))


def test_run_exits_0_only_when_a_summary_was_posted_with_something_in_it(
    db: Path, monkeypatch
) -> None:
    assignment_id = _add(db, "--notify", "x")
    _outcome(monkeypatch, posted=True, usable=2)

    assert main(["--db", str(db), "run", assignment_id]) == 0


def test_a_run_that_found_nothing_is_not_a_success(capsys, db: Path, monkeypatch) -> None:
    """Silence and success must not look alike to whatever fired this."""
    assignment_id = _add(db, "--notify", "x")
    _outcome(monkeypatch, posted=True, usable=0)

    assert main(["--db", str(db), "run", assignment_id]) == 1
    assert "nothing usable" in capsys.readouterr().err


def test_a_report_that_never_reached_discord_has_its_own_exit_code(
    capsys, db: Path, monkeypatch
) -> None:
    assignment_id = _add(db, "--notify", "x")
    _outcome(monkeypatch, posted=False, usable=5)

    assert main(["--db", str(db), "run", assignment_id]) == 3
    assert "could not be posted" in capsys.readouterr().err


def test_no_post_is_judged_on_findings_alone(db: Path, monkeypatch) -> None:
    assignment_id = _add(db, "--notify", "x")
    _outcome(monkeypatch, posted=False, usable=1)
    assert main(["--db", str(db), "run", assignment_id, "--no-post"]) == 0
    _outcome(monkeypatch, posted=False, usable=0)
    assert main(["--db", str(db), "run", assignment_id, "--no-post"]) == 1


# -- unschedule -----------------------------------------------------------


def test_unschedule_drops_the_timer_but_keeps_the_assignment_runnable(
    db: Path, monkeypatch
) -> None:
    """For when an external scheduler owns the timing instead of track."""
    cancelled: list = []
    monkeypatch.setattr(
        "track.cli.cancel_schedule", lambda job, backend, **kw: cancelled.append(job)
    )
    assignment_id = _add(db, "--notify", "x")

    assert main(["--db", str(db), "unschedule", assignment_id]) == 0
    with Store(db) as store:
        a = store.get_assignment(assignment_id)

    assert cancelled == ["job-1"]
    assert a is not None
    assert a.status == "active", "still runnable on demand"
    assert a.job_id is None and a.backend is None


def test_an_unscheduled_assignment_still_runs_without_force(db: Path, monkeypatch) -> None:
    assignment_id = _add(db, "--notify", "x")
    main(["--db", str(db), "unschedule", assignment_id])
    _outcome(monkeypatch)

    assert main(["--db", str(db), "run", assignment_id]) == 0


# -- run --all-active ----------------------------------------------------


def _result(*, usable: int = 1, posted: bool = True, summary: str = "s") -> RunOutcome:
    return RunOutcome(
        findings=[], summary=summary, posted=posted, usable=usable, scout_failures=0
    )


def test_run_needs_either_an_id_or_all_active(capsys, db: Path) -> None:
    assert main(["--db", str(db), "run"]) == 2
    assert main(["--db", str(db), "run", "abc", "--all-active"]) == 2


def test_all_active_runs_every_active_assignment(capsys, db: Path, monkeypatch) -> None:
    first, second = _add(db), _add(db)
    ran: list[str] = []

    def fake(store, assignment, **kwargs):
        ran.append(assignment.id)
        return _result(summary=f"summary for {assignment.id}")

    monkeypatch.setattr("track.cli.run_assignment", fake)
    capsys.readouterr()

    assert main(["--db", str(db), "run", "--all-active"]) == 0
    assert sorted(ran) == sorted([first, second])
    assert f"summary for {second}" in capsys.readouterr().out


def test_all_active_skips_paused_assignments(capsys, db: Path, monkeypatch) -> None:
    active, paused = _add(db), _add(db)
    assert main(["--db", str(db), "pause", paused]) == 0
    ran: list[str] = []
    monkeypatch.setattr(
        "track.cli.run_assignment",
        lambda store, a, **k: (ran.append(a.id), _result())[1],
    )

    assert main(["--db", str(db), "run", "--all-active"]) == 0
    assert ran == [active]


def test_one_failing_assignment_does_not_cancel_the_others(
    capsys, db: Path, monkeypatch
) -> None:
    """A failing laptop hunt must not silently take the GPU hunt with it."""
    first, second = _add(db), _add(db)
    ran: list[str] = []

    def fake(store, assignment, **kwargs):
        ran.append(assignment.id)
        if assignment.id == first:
            raise TrackError("scouts all died")
        return _result()

    monkeypatch.setattr("track.cli.run_assignment", fake)

    code = main(["--db", str(db), "run", "--all-active"])

    assert sorted(ran) == sorted([first, second])
    assert code == 3


def test_an_unposted_summary_outranks_a_merely_empty_one(db: Path, monkeypatch) -> None:
    """One silent assignment out of two is still a silent assignment."""
    _add(db), _add(db)
    outcomes = iter([_result(usable=0, posted=False), _result(usable=5)])
    monkeypatch.setattr("track.cli.run_assignment", lambda *a, **k: next(outcomes))

    assert main(["--db", str(db), "run", "--all-active"]) == 3


def test_all_active_reports_1_when_nothing_was_usable(db: Path, monkeypatch) -> None:
    _add(db), _add(db)
    monkeypatch.setattr("track.cli.run_assignment", lambda *a, **k: _result(usable=0))

    assert main(["--db", str(db), "run", "--all-active"]) == 1


def test_all_active_with_no_active_assignments_is_not_success(db: Path) -> None:
    assert main(["--db", str(db), "run", "--all-active"]) == 1


def test_no_schedule_does_not_claim_an_interval(capsys, db: Path) -> None:
    capsys.readouterr()
    main(["--db", str(db), "add", "a laptop", "--interval", "6h", "--no-schedule"])
    err = capsys.readouterr().err
    assert "every 6h" not in err
    assert "not scheduled" in err


def test_show_explains_why_a_find_was_recommended(capsys, db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    with Store(db) as store:
        run_id = store.start_run(assignment_id)
        store.add_finding(
            assignment_id, run_id, "KP", "ThinkPad T490", 230.0, "EUR", "https://kp/1",
            "k1", 0.82, True, reference_price=300.0, reference_n=4, score_basis="mispricing",
            rationale="Cheapest T490 with 512GB; three others sit at 280-300 EUR.",
            condition="used", product_year=2019,
        )
    capsys.readouterr()

    main(["--db", str(db), "show", assignment_id])
    out = capsys.readouterr().out

    assert "score 0.82 mispricing" in out
    assert "vs 300.00 EUR from 4 comparable(s)" in out
    assert "Cheapest T490 with 512GB" in out
    assert "2019 model" in out
    assert "first seen" in out


def test_show_lists_what_has_been_retired(capsys, db: Path) -> None:
    assignment_id = _add(db, "--notify", "x")
    with Store(db) as store:
        run_id = store.start_run(assignment_id)
        store.add_finding(
            assignment_id, run_id, "KP", "ThinkPad T490", 230.0, "EUR", "https://kp/1",
            "k1", 0.8, True,
        )
        store.retire(assignment_id, "k1", reason="gone", note="oglas je prodat")
    capsys.readouterr()

    main(["--db", str(db), "show", assignment_id])
    out = capsys.readouterr().out

    assert "retired (1)" in out
    assert "[gone]" in out
    assert "oglas je prodat" in out


# -- the web half --------------------------------------------------------


def test_web_missing_is_an_error_not_a_crash(capsys, db: Path, monkeypatch) -> None:
    """Every scheduler box installs track without the web extra."""
    monkeypatch.setattr(
        "track.cli.importlib.import_module",
        lambda name: (_ for _ in ()).throw(ImportError(f"No module named {name!r}")),
    )
    code = main(["--db", str(db), "web"])
    err = capsys.readouterr().err

    assert code == 1
    assert "not available" in err
    assert ".[web]" in err


def test_a_web_module_without_an_entry_point_is_an_error_not_a_crash(
    capsys, db: Path, monkeypatch
) -> None:
    """A half-written track.web must not raise out of `track web`."""
    monkeypatch.setattr(
        "track.cli.importlib.import_module", lambda name: types.SimpleNamespace()
    )

    assert main(["--db", str(db), "web"]) == 1
    assert "not available" in capsys.readouterr().err


def test_the_web_module_owns_its_own_flags(capsys, db: Path, monkeypatch) -> None:
    """So adding one never means editing cli.py."""
    seen: dict[str, object] = {}

    def fake_main(argv, *, db_path, log):
        seen["argv"] = argv
        seen["db_path"] = db_path
        return 0

    monkeypatch.setattr(
        "track.cli.importlib.import_module",
        lambda name: types.SimpleNamespace(main=fake_main),
    )

    assert main(["--db", str(db), "web", "--port", "8080", "--open"]) == 0
    assert seen["argv"] == ["--port", "8080", "--open"]
    assert Path(str(seen["db_path"])) == db


def test_run_still_works_with_the_web_subcommand_registered(db: Path, monkeypatch) -> None:
    """The 06:05 run is the thing that must not break."""
    assignment_id = _add(db, "--notify", "x")
    monkeypatch.setattr("track.cli.run_assignment", lambda *a, **k: _result())

    assert main(["--db", str(db), "run", assignment_id, "--no-post"]) == 0


def test_the_verb_list_used_for_splitting_matches_the_parser() -> None:
    """_split_web_args works off a hand-written set; this pins it to reality."""
    parser = _build_parser()
    registered = {
        name
        for action in parser._subparsers._group_actions  # type: ignore[union-attr]
        for name in action.choices
    }
    assert registered == set(SUBCOMMANDS)


def test_a_value_that_reads_like_a_verb_is_not_mistaken_for_one(capsys, db: Path) -> None:
    """`--db /tmp/web` must not turn into `track web`."""
    tokens, extra = _split_web_args(["--db", "web", "list"], SUBCOMMANDS)
    assert extra == []
    assert tokens == ["--db", "web", "list"]


def test_everything_after_the_web_verb_is_passed_through_untouched() -> None:
    tokens, extra = _split_web_args(
        ["--db", "x.db", "web", "--port", "8080", "--open"], SUBCOMMANDS
    )
    assert tokens == ["--db", "x.db", "web"]
    assert extra == ["--port", "8080", "--open"]


# -- choosing a check time -----------------------------------------------


def _advice(monkeypatch, check_at: str = "07:30", why: str = "sellers post overnight") -> list[str]:
    """Stand in for the Sonnet advisor and record what it was asked about."""
    asked: list[str] = []

    def fake(text: str, **kwargs: object):
        asked.append(text)
        return ScheduleAdvice(check_at=check_at, rationale=why, cost_usd=0.01)

    monkeypatch.setattr("track.cli.scouts.recommend_check_time", fake)
    return asked


def test_interval_and_at_together_are_a_usage_error(capsys, db: Path) -> None:
    """They are alternatives, not layers, and --help says so."""
    rc = main(["--db", str(db), "add", "x", "--interval", "6h", "--at", "08:00"])
    assert rc == 2
    assert "alternatives" in capsys.readouterr().err


def test_an_explicit_time_is_taken_as_given_and_skips_the_advisor(
    monkeypatch, db: Path
) -> None:
    asked = _advice(monkeypatch)
    assignment_id = _add(db, "--at", "8:00")

    with Store(db) as store:
        row = store.get_assignment(assignment_id)
    assert row is not None
    assert row.check_at == "08:00"
    assert row.check_at_source == "user"
    assert asked == []


def test_an_explicit_interval_also_skips_the_advisor(monkeypatch, db: Path) -> None:
    asked = _advice(monkeypatch)
    assignment_id = _add(db, "--interval", "2h")

    with Store(db) as store:
        row = store.get_assignment(assignment_id)
    assert row is not None
    assert row.check_at is None
    assert row.interval_seconds == 7200
    assert asked == []


def test_with_neither_the_advisor_picks_the_time_and_its_reason_is_kept(
    monkeypatch, capsys, db: Path
) -> None:
    _advice(monkeypatch, "07:30", "sellers post overnight")
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--db", str(db), "add", "a laptop"]) == 0
    assignment_id = out.getvalue().strip()

    with Store(db) as store:
        row = store.get_assignment(assignment_id)
    assert row is not None
    assert row.check_at == "07:30"
    assert row.check_at_source == "agent"
    assert row.check_at_rationale == "sellers post overnight"
    # The reason is told to the operator at the moment it is chosen, not
    # only kept for later.
    assert "sellers post overnight" in capsys.readouterr().err


def test_an_advisor_that_fails_costs_quality_not_function(
    monkeypatch, capsys, db: Path
) -> None:
    """The whole degradation rule, in one test.

    No opinion about the best hour must never stop an assignment being
    tracked -- it falls back to exactly the flat interval that used to be
    the default, and says that it did.
    """
    def boom(text: str, **kwargs: object):
        raise ScoutError("claude CLI not found on PATH")

    monkeypatch.setattr("track.cli.scouts.recommend_check_time", boom)
    assignment_id = _add(db)  # _add passes --no-advise

    monkeypatch.setattr("track.cli.scouts.recommend_check_time", boom)
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--db", str(db), "add", "a GPU"]) == 0
    fell_back = out.getvalue().strip()

    with Store(db) as store:
        row = store.get_assignment(fell_back)
    assert row is not None
    assert row.check_at is None
    assert row.interval_seconds == 6 * 3600
    err = capsys.readouterr().err
    assert "could not work out the best time" in err
    assert assignment_id  # the --no-advise one was unaffected


def test_no_advise_does_not_call_the_advisor(monkeypatch, db: Path) -> None:
    asked = _advice(monkeypatch)
    _add(db)
    assert asked == []


def test_a_usage_error_is_caught_before_the_advisor_is_paid_for(
    monkeypatch, db: Path
) -> None:
    """Spending a Sonnet call to then reject the command line is pure waste."""
    asked = _advice(monkeypatch)
    assert main(["--db", str(db), "add", "x", "--wake-backend", "wol"]) == 2
    assert asked == []


def test_show_prints_why_the_time_was_chosen(monkeypatch, capsys, db: Path) -> None:
    _advice(monkeypatch, "07:30", "sellers post overnight")
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--db", str(db), "add", "a laptop"])
    assignment_id = out.getvalue().strip()
    capsys.readouterr()

    assert main(["--db", str(db), "show", assignment_id]) == 0
    shown = capsys.readouterr().out
    assert "daily at 07:30" in shown
    assert "sellers post overnight" in shown
    assert "advisor" in shown


# -- slots, end to end through the CLI ------------------------------------


def test_run_takes_exactly_one_selector(capsys, db: Path) -> None:
    assert main(["--db", str(db), "run"]) == 2
    assert main(["--db", str(db), "run", "abc", "--all-active"]) == 2
    assert main(["--db", str(db), "run", "--all-active", "--slot", "08:00"]) == 2
    assert "exactly one" in capsys.readouterr().err


def test_a_slot_run_runs_only_that_slot(monkeypatch, db: Path) -> None:
    ran: list[str] = []

    def fake_run(store, assignment, **kwargs):
        ran.append(assignment.id)
        return RunOutcome([], "summary", True, 1, 0)

    monkeypatch.setattr("track.cli.run_assignment", fake_run)
    monkeypatch.setattr("track.cli.slots.arm", lambda *a, **k: None)
    monkeypatch.setattr("track.cli.wake_supports_every", lambda **k: False)

    morning = _add(db, "--at", "08:00")
    _add(db, "--at", "19:00")

    assert main(["--db", str(db), "run", "--slot", "08:00"]) == 0
    assert ran == [morning]


def test_pausing_a_member_rebuilds_the_slot_without_it(monkeypatch, db: Path) -> None:
    armed: list[tuple[str, int]] = []

    def recording_arm(store, check_at, run_cmd, **kwargs):
        armed.append((check_at, len(store.slot_members(check_at))))
        return fake_arm(store, check_at, run_cmd, **kwargs)

    monkeypatch.setattr("track.cli.slots.arm", recording_arm)
    _add(db, "--at", "08:00")
    stays = _add(db, "--at", "08:00")
    assert armed == [("08:00", 1), ("08:00", 2)]

    assert main(["--db", str(db), "pause", stays]) == 0
    # Re-armed for the one that is left, not cancelled.
    assert armed[-1] == ("08:00", 1)


def test_removing_the_last_member_cancels_the_shared_task(monkeypatch, db: Path) -> None:
    cancelled: list[str] = []
    monkeypatch.setattr(
        "track.cli.cancel_schedule", lambda job_id, backend, **k: cancelled.append(job_id)
    )

    only = _add(db, "--at", "08:00")
    assert main(["--db", str(db), "remove", only]) == 0
    assert cancelled == ["track-slot-0800"]


def test_unscheduling_a_member_detaches_it_but_keeps_the_reasoning(
    monkeypatch, db: Path
) -> None:
    _advice(monkeypatch, "07:30", "sellers post overnight")
    monkeypatch.setattr("track.cli.slots.arm", lambda *a, **k: None)
    monkeypatch.setattr("track.cli.cancel_schedule", lambda *a, **k: None)
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--db", str(db), "add", "a laptop"])
    assignment_id = out.getvalue().strip()

    assert main(["--db", str(db), "unschedule", assignment_id]) == 0
    with Store(db) as store:
        row = store.get_assignment(assignment_id)
    assert row is not None
    assert row.status == "active"
    assert row.check_at is None
    assert row.check_at_rationale == "sellers post overnight"
