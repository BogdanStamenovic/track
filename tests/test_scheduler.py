"""Scheduler tests.

The interesting cases are the two that bite in production: re-arming must
not be able to leave two timers racing, and waking a *sleeping* machine
needs a second task because rtcwake/WoL only resume a box, they don't run
anything on it.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from conftest import completed

from track.errors import SchedulerError
from track.scheduler import (
    MIN_LEAD_SECONDS,
    RESUME_GRACE_SECONDS,
    cancel,
    resume_task_id_for,
    schedule,
    task_id_for,
)

NOW = 1_000_000.0


def _recording(stdout: str = "job-1") -> tuple[list[list[str]], Any]:
    calls: list[list[str]] = []

    def runner(cmd: list[str], *, timeout: int):
        calls.append(cmd)
        return completed(stdout)

    return calls, runner


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


# -- the shell path ------------------------------------------------------


def test_shell_backend_schedules_one_task_at_the_interval() -> None:
    calls, runner = _recording()
    result = schedule(
        "a1", 3600, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW
    )

    assert len(calls) == 1
    assert _flag(calls[0], "--at") == str(int(NOW + 3600))
    assert _flag(calls[0], "--task") == "track run a1"
    assert _flag(calls[0], "--backend") == "shell"
    assert result.backend == "wake"
    assert result.resume_job_id is None


def test_rearming_reuses_a_fixed_task_id_so_timers_cannot_pile_up() -> None:
    calls, runner = _recording()
    for _ in range(3):
        schedule("a1", 3600, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW)

    assert {_flag(c, "--id") for c in calls} == {task_id_for("a1")}


def test_next_run_at_is_iso_utc() -> None:
    _calls, runner = _recording()
    result = schedule(
        "a1", 60, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW
    )
    assert result.next_run_at.endswith("+00:00")


# -- waking a sleeping machine -------------------------------------------


def test_rtcwake_schedules_a_resume_and_a_run_not_just_a_resume() -> None:
    """rtcwake only brings the box back; something still has to run track."""
    calls, runner = _recording()
    result = schedule(
        "a1",
        3600,
        ["track", "run", "a1"],
        wake_backend="rtcwake",
        runner=runner,
        wake_available=True,
        now=NOW,
    )
    resume, run = calls

    assert _flag(resume, "--backend") == "rtcwake"
    assert _flag(resume, "--id") == resume_task_id_for("a1")
    assert _flag(run, "--backend") == "shell"
    assert _flag(run, "--task") == "track run a1"
    assert result.resume_job_id is not None


def test_the_run_task_fires_after_the_machine_has_had_time_to_come_up() -> None:
    calls, runner = _recording()
    schedule(
        "a1",
        3600,
        ["track", "run", "a1"],
        wake_backend="rtcwake",
        runner=runner,
        wake_available=True,
        now=NOW,
    )
    resume_at = int(_flag(calls[0], "--at"))
    run_at = int(_flag(calls[1], "--at"))

    assert run_at - resume_at == RESUME_GRACE_SECONDS


def test_wol_passes_the_mac_through_as_the_target() -> None:
    calls, runner = _recording()
    schedule(
        "a1",
        3600,
        ["track", "run", "a1"],
        wake_backend="wol",
        target="a8:a1:59:fd:4d:13",
        runner=runner,
        wake_available=True,
        now=NOW,
    )
    assert _flag(calls[0], "--target") == "a8:a1:59:fd:4d:13"


def test_wol_without_a_mac_is_rejected_before_anything_is_scheduled() -> None:
    calls, runner = _recording()
    with pytest.raises(SchedulerError, match="wake-target"):
        schedule("a1", 3600, ["track"], wake_backend="wol", runner=runner, wake_available=True)
    assert calls == []


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(SchedulerError, match="unknown wake backend"):
        schedule("a1", 3600, ["track"], wake_backend="telepathy", wake_available=True)


# -- the systemd fallback ------------------------------------------------


def test_systemd_fallback_is_used_when_wake_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "track.scheduler.subprocess.run",
        lambda *a, **k: completed("", returncode=0),
    )

    result = schedule("a1", 3600, ["track", "run", "a1"], wake_available=False, now=NOW)

    assert result.backend == "systemd-timer"
    assert result.job_id == "track-a1.timer"
    unit = tmp_path / ".config" / "systemd" / "user" / "track-a1.timer"
    body = unit.read_text()
    assert "OnUnitActiveSec=3600s" in body
    assert "OnActiveSec=0s" in body, "this is what catches up a run missed while off"
    assert "Persistent=" not in body, "only affects OnCalendar timers; misleading here"


def test_systemd_fallback_refuses_a_resume_backend_it_cannot_honour() -> None:
    """A user timer cannot wake a suspended machine; saying so beats pretending."""
    with pytest.raises(SchedulerError, match="cannot wake a sleeping machine"):
        schedule("a1", 3600, ["track"], wake_backend="rtcwake", wake_available=False)


# -- failure modes -------------------------------------------------------


def test_wake_failure_is_a_scheduler_error() -> None:
    def runner(cmd, *, timeout):
        return completed("", returncode=1, stderr="wake: bad --at")

    with pytest.raises(SchedulerError, match="wake add failed"):
        schedule("a1", 3600, ["track"], runner=runner, wake_available=True)


def test_silent_wake_success_is_still_an_error() -> None:
    def runner(cmd, *, timeout):
        return completed("   ")

    with pytest.raises(SchedulerError, match="no task id"):
        schedule("a1", 3600, ["track"], runner=runner, wake_available=True)


def test_wake_timeout_is_a_scheduler_error() -> None:
    def runner(cmd, *, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with pytest.raises(SchedulerError, match="timed out"):
        schedule("a1", 3600, ["track"], runner=runner, wake_available=True)


# -- cancel --------------------------------------------------------------


def test_cancel_removes_the_paired_resume_task_too() -> None:
    calls, runner = _recording()
    cancel("job-1", "wake", resume_job_id="job-1-resume", runner=runner)

    assert [c[-1] for c in calls] == ["job-1", "job-1-resume"]


def test_cancel_without_a_pair_cancels_once() -> None:
    calls, runner = _recording()
    cancel("job-1", "wake", runner=runner)
    assert len(calls) == 1


def test_cancel_of_an_unknown_backend_is_an_error() -> None:
    with pytest.raises(SchedulerError, match="unknown scheduler backend"):
        cancel("job-1", "carrier-pigeon")


# -- which machine runs it -----------------------------------------------


def test_the_run_task_can_name_the_machine_that_should_run_it() -> None:
    """A woken box must run the check; the wake server running it is the bug."""
    calls, runner = _recording()
    schedule(
        "a1",
        3600,
        ["track", "run", "a1"],
        wake_backend="wol",
        target="aa:bb:cc:dd:ee:ff",
        run_on="archserver",
        runner=runner,
        wake_available=True,
        now=NOW,
    )
    resume, run = calls

    assert "--on" not in resume, "the server sends the magic packet, not the sleeping box"
    assert _flag(run, "--on") == "archserver"


def test_without_run_on_the_task_is_unowned() -> None:
    calls, runner = _recording()
    schedule("a1", 3600, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW)
    assert "--on" not in calls[0]


def test_at_is_always_epoch_seconds_never_a_bare_relative_offset() -> None:
    """wake rejects "+5"; it used to read it as 1970 and fire immediately."""
    calls, runner = _recording()
    schedule("a1", 3600, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW)
    at = _flag(calls[0], "--at")

    assert not at.startswith("+")
    assert int(at) == int(NOW) + 3600


def test_cancelling_a_timer_stops_and_resets_its_service_too(monkeypatch) -> None:
    """Disabling a timer does not stop the service it triggers."""
    from track.scheduler import _cancel_systemd_timer

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "track.scheduler.subprocess.run",
        lambda cmd, **kw: calls.append(cmd) or completed(""),
    )
    monkeypatch.setattr("pathlib.Path.unlink", lambda self, missing_ok=False: None)

    _cancel_systemd_timer("track-a1.timer")
    verbs = [(c[2], c[-1]) for c in calls]

    assert ("disable", "track-a1.timer") in verbs
    assert ("stop", "track-a1.service") in verbs
    assert ("reset-failed", "track-a1.service") in verbs
    assert calls[-1][2] == "daemon-reload", "reload last, after the files are gone"


# -- never schedule something already due --------------------------------


def test_a_zero_interval_cannot_schedule_a_run_in_the_past() -> None:
    """Every run re-arms the next, so a past-dated one spins, it doesn't fire once."""
    calls, runner = _recording()
    schedule("a1", 0, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW)

    assert int(_flag(calls[0], "--at")) == int(NOW) + MIN_LEAD_SECONDS


def test_a_negative_interval_is_floored_too() -> None:
    calls, runner = _recording()
    schedule("a1", -3600, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW)

    assert int(_flag(calls[0], "--at")) > int(NOW)


def test_a_normal_interval_is_left_alone() -> None:
    calls, runner = _recording()
    schedule("a1", 3600, ["track", "run", "a1"], runner=runner, wake_available=True, now=NOW)

    assert int(_flag(calls[0], "--at")) == int(NOW) + 3600


def test_the_systemd_fallback_is_floored_as_well(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "track.scheduler.subprocess.run", lambda *a, **k: completed("", returncode=0)
    )

    schedule("a1", 0, ["track", "run", "a1"], wake_available=False, now=NOW)

    unit = (tmp_path / ".config" / "systemd" / "user" / "track-a1.timer").read_text()
    assert f"OnUnitActiveSec={MIN_LEAD_SECONDS}s" in unit


def test_cancelling_a_timer_removes_its_persistence_stamp(tmp_path, monkeypatch) -> None:
    """Systemd writes one per timer and never removes it with the unit."""
    from track.scheduler import _cancel_systemd_timer

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "track.scheduler.subprocess.run", lambda *a, **k: completed("")
    )
    stamp = tmp_path / ".local" / "share" / "systemd" / "timers" / "stamp-track-a1.timer"
    stamp.parent.mkdir(parents=True)
    stamp.touch()

    _cancel_systemd_timer("track-a1.timer")

    assert not stamp.exists()
