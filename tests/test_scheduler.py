from __future__ import annotations

import subprocess

import pytest

from track.errors import SchedulerError
from track.scheduler import cancel, schedule


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_schedule_via_wake_returns_job_id() -> None:
    def runner(cmd, *, timeout):
        assert cmd[:2] == ["wake", "add"]
        return _completed("task-42\n")

    result = schedule(
        "assign1", 3600, ["track", "run", "assign1"], runner=runner, wake_available=True, now=1000.0
    )
    assert result.job_id == "task-42"
    assert result.backend == "wake"


def test_schedule_via_wake_passes_computed_epoch() -> None:
    seen_cmd = {}

    def runner(cmd, *, timeout):
        seen_cmd["cmd"] = cmd
        return _completed("task-1")

    schedule("a", 3600, ["track", "run", "a"], runner=runner, wake_available=True, now=1000.0)
    at_index = seen_cmd["cmd"].index("--at") + 1
    assert seen_cmd["cmd"][at_index] == "4600"


def test_schedule_via_wake_nonzero_exit_raises() -> None:
    def runner(cmd, *, timeout):
        return _completed("", returncode=1, stderr="wake is not ready")

    with pytest.raises(SchedulerError, match="wake is not ready"):
        schedule("a", 3600, ["track", "run", "a"], runner=runner, wake_available=True)


def test_schedule_via_wake_empty_output_raises() -> None:
    def runner(cmd, *, timeout):
        return _completed("")

    with pytest.raises(SchedulerError, match="no task id"):
        schedule("a", 3600, ["track", "run", "a"], runner=runner, wake_available=True)


def test_schedule_wake_missing_binary_raises() -> None:
    def runner(cmd, *, timeout):
        raise FileNotFoundError()

    with pytest.raises(SchedulerError, match="not found"):
        schedule("a", 3600, ["track", "run", "a"], runner=runner, wake_available=True)


def test_cancel_wake_delegates_to_runner() -> None:
    calls = []

    def runner(cmd, *, timeout):
        calls.append(cmd)
        return _completed("")

    cancel("task-1", "wake", runner=runner)
    assert calls == [["wake", "cancel", "task-1"]]


def test_cancel_unknown_backend_raises() -> None:
    with pytest.raises(SchedulerError, match="unknown"):
        cancel("job", "carrier-pigeon")


def test_schedule_systemd_fallback_when_wake_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        return _completed("")

    monkeypatch.setattr("track.scheduler.subprocess.run", fake_run)

    result = schedule(
        "assign2", 1800, ["track", "run", "assign2"], wake_available=False, now=1000.0
    )
    assert result.backend == "systemd-timer"
    assert result.job_id == "track-assign2.timer"

    timer_path = tmp_path / ".config" / "systemd" / "user" / "track-assign2.timer"
    service_path = tmp_path / ".config" / "systemd" / "user" / "track-assign2.service"
    assert timer_path.exists()
    assert service_path.exists()
    assert "OnUnitActiveSec=1800s" in timer_path.read_text()
