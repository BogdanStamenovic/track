"""Wakeup scheduling for track.

Primary path shells out to the sibling `wake` CLI. `wake` tasks are
one-shot: `wake add --at <epoch-seconds> --task <cmd> --backend shell`
fires once and is done -- there is no recurring syntax. Recurrence is
track's job, not wake's: each `track run` invocation that used the wake
backend re-arms its own successor with a fresh `wake add` right before it
finishes (see engine.run_assignment). This matches wake's own contract:
it fires once at a time and callers own their repeat policy.

If `wake` isn't on PATH (true as of writing -- it's still being scaffolded
by a sibling agent), falls back to a systemd --user timer, which recurs on
its own via OnUnitActiveSec and needs no re-arming.

`_invoke_wake_add` / `_invoke_wake_cancel` are the only places that know
wake's CLI shape -- when its contract changes, only these need to change.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .errors import SchedulerError

WAKE_BIN = "wake"


class Runner(Protocol):
    def __call__(self, cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]: ...


def _default_runner(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    job_id: str
    backend: str  # "wake" | "systemd-timer"
    next_run_at: str  # ISO8601, best-effort


def _isoformat(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _invoke_wake_add(
    run_at_epoch: int,
    run_cmd: list[str],
    *,
    backend: str,
    target: str | None,
    runner: Runner,
    timeout: int,
) -> str:
    cmd = [
        WAKE_BIN,
        "add",
        "--at",
        str(run_at_epoch),
        "--task",
        " ".join(run_cmd),
        "--backend",
        backend,
    ]
    if target:
        cmd += ["--target", target]
    try:
        result = runner(cmd, timeout=timeout)
    except FileNotFoundError as exc:
        raise SchedulerError("wake CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SchedulerError(f"wake add timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise SchedulerError(f"wake add failed: {result.stderr.strip()[:500]}")
    task_id = result.stdout.strip()
    if not task_id:
        raise SchedulerError("wake add returned no task id")
    return task_id


def _invoke_wake_cancel(task_id: str, *, runner: Runner, timeout: int) -> None:
    try:
        result = runner([WAKE_BIN, "cancel", task_id], timeout=timeout)
    except FileNotFoundError as exc:
        raise SchedulerError("wake CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SchedulerError(f"wake cancel timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise SchedulerError(f"wake cancel failed: {result.stderr.strip()[:500]}")


def _systemd_unit_paths(label: str) -> tuple[Path, Path]:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    return unit_dir / f"track-{label}.service", unit_dir / f"track-{label}.timer"


def _schedule_systemd_timer(label: str, interval_seconds: int, run_cmd: list[str]) -> str:
    service_path, timer_path = _systemd_unit_paths(label)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(
        "[Unit]\n"
        f"Description=track assignment {label}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={' '.join(run_cmd)}\n"
    )
    timer_path.write_text(
        "[Unit]\n"
        f"Description=track timer for {label}\n\n"
        "[Timer]\n"
        f"OnUnitActiveSec={interval_seconds}s\n"
        "OnActiveSec=0s\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, check=False
        )
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", timer_path.name],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SchedulerError("neither `wake` nor systemctl are available") from exc
    if result.returncode != 0:
        raise SchedulerError(f"systemctl enable failed: {result.stderr.strip()[:500]}")
    return timer_path.name


def _cancel_systemd_timer(job_id: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", job_id],
        capture_output=True,
        text=True,
        check=False,
    )
    label = job_id.removeprefix("track-").removesuffix(".timer")
    service_path, timer_path = _systemd_unit_paths(label)
    service_path.unlink(missing_ok=True)
    timer_path.unlink(missing_ok=True)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, check=False
    )


def schedule(
    assignment_id: str,
    interval_seconds: int,
    run_cmd: list[str],
    *,
    backend: str = "shell",
    target: str | None = None,
    runner: Runner = _default_runner,
    timeout: int = 30,
    wake_available: bool | None = None,
    now: float | None = None,
) -> ScheduleResult:
    """Arm the next wakeup for an assignment, `interval_seconds` from now."""
    if wake_available is None:
        wake_available = shutil.which(WAKE_BIN) is not None
    run_at = int((now if now is not None else time.time()) + interval_seconds)
    if wake_available:
        job_id = _invoke_wake_add(
            run_at, run_cmd, backend=backend, target=target, runner=runner, timeout=timeout
        )
        return ScheduleResult(job_id=job_id, backend="wake", next_run_at=_isoformat(run_at))
    job_id = _schedule_systemd_timer(assignment_id, interval_seconds, run_cmd)
    return ScheduleResult(job_id=job_id, backend="systemd-timer", next_run_at=_isoformat(run_at))


def cancel(
    job_id: str, backend: str, *, runner: Runner = _default_runner, timeout: int = 30
) -> None:
    if backend == "wake":
        _invoke_wake_cancel(job_id, runner=runner, timeout=timeout)
    elif backend == "systemd-timer":
        _cancel_systemd_timer(job_id)
    else:
        raise SchedulerError(f"unknown scheduler backend: {backend!r}")
