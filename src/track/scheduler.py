"""Wakeup scheduling for track.

The primary path shells out to the sibling `wake` CLI. `wake` tasks are
one-shot -- there is no recurring syntax -- so recurrence is track's job:
every `track run` that used the wake backend re-arms its own successor right
before it finishes (see engine.run_assignment). Re-arming reuses a fixed task
id (`track-<assignment>`), which makes it idempotent: an interrupted run that
already armed its successor cannot leave two timers racing on the next one.

Waking a *sleeping* machine takes two tasks, not one. `rtcwake` and
Wake-on-LAN only bring a box back to life; neither runs anything once it is
up, and `wake serve` cannot fire a shell task on a machine that is suspended.
So an assignment with a resume backend schedules a pair: the resume task at
T, and the shell task that actually runs track at T + RESUME_GRACE_SECONDS,
by which time the machine is up to receive it.

Which machine runs the task matters once a resume backend is involved.
`wake` fires shell tasks on the server unless the task names an owner, so a
Wake-on-LAN assignment left unowned would wake the target box and then run
track on the server instead of on it. `run_on` passes wake's `--on <host>`
for the run task to close that.

If `wake` isn't on PATH, this falls back to a `systemd --user` timer, which
recurs on its own and needs no re-arming -- but only helps a box that is
already running. That is the honest limit of the fallback, not a bug in it.

`_invoke_wake_*` are the only places that know wake's CLI shape; when its
contract changes, only they need to.
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

# How long after the resume task fires before the run task does. A cold
# resume from suspend plus network coming back is comfortably inside this;
# the cost of it being generous is only that the run starts late.
RESUME_GRACE_SECONDS = 120

RESUME_BACKENDS = ("rtcwake", "wol")

# Never ask for a wakeup that is already due. A scheduler asked to run
# something at a past time runs it immediately, and since every run re-arms
# the next one, a past-dated re-arm does not fire once -- it spins as fast as
# the scheduler polls. Nothing in normal operation produces one (intervals
# are validated positive at the CLI), which is exactly why it is worth a
# floor: the ways to get here are a backwards clock step, a zero interval in
# a hand-edited row, and whatever else turns up at 3am.
MIN_LEAD_SECONDS = 60


class Runner(Protocol):
    def __call__(self, cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]: ...


def _default_runner(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    job_id: str
    backend: str  # "wake" | "systemd-timer"
    next_run_at: str  # ISO8601, best-effort
    resume_job_id: str | None = None


def _floor_at(base: float, interval_seconds: int) -> int:
    """The next fire time, never sooner than MIN_LEAD_SECONDS after `base`.

    Floored against the same clock the interval is measured from, rather than
    against the wall clock: a caller that injects a clock means it, and a
    scheduler reading a skewed clock is still consistent with itself. What
    this guards is a non-positive interval reaching the arithmetic.
    """
    return int(base + max(interval_seconds, MIN_LEAD_SECONDS))


def _isoformat(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _invoke_wake_add(
    run_at_epoch: int,
    task: str,
    *,
    backend: str,
    target: str | None,
    task_id: str | None,
    runner: Runner,
    timeout: int,
    run_on: str | None = None,
) -> str:
    # --at is always epoch seconds, never a relative offset: wake rejects a
    # bare "+5" outright and used to read it as 1970, firing immediately.
    cmd = [WAKE_BIN, "add", "--at", str(run_at_epoch), "--task", task, "--backend", backend]
    if target:
        cmd += ["--target", target]
    if task_id:
        cmd += ["--id", task_id]
    if run_on:
        cmd += ["--on", run_on]
    try:
        result = runner(cmd, timeout=timeout)
    except FileNotFoundError as exc:
        raise SchedulerError("wake CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SchedulerError(f"wake add timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise SchedulerError(f"wake add failed: {result.stderr.strip()[:500]}")
    returned_id = result.stdout.strip()
    if not returned_id:
        raise SchedulerError("wake add returned no task id")
    return returned_id


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
    """Disable the timer and take its service with it.

    Disabling the timer stops the timer, not the service it triggers. A
    service left running, mid-activation, or failed keeps a runtime entry
    after its unit file is deleted -- systemd then holds a job for a unit it
    can no longer find, which needs a manual `reset-failed` to clear.
    Observed both, so the service is stopped and reset before the files go.
    """
    label = job_id.removeprefix("track-").removesuffix(".timer")
    service = f"track-{label}.service"
    for argv in (
        ["disable", "--now", job_id],
        ["stop", service],
        ["reset-failed", service],
    ):
        subprocess.run(
            ["systemctl", "--user", *argv], capture_output=True, text=True, check=False
        )
    service_path, timer_path = _systemd_unit_paths(label)
    service_path.unlink(missing_ok=True)
    timer_path.unlink(missing_ok=True)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, check=False
    )


def task_id_for(assignment_id: str) -> str:
    return f"track-{assignment_id}"


def resume_task_id_for(assignment_id: str) -> str:
    return f"track-{assignment_id}-resume"


def schedule(
    assignment_id: str,
    interval_seconds: int,
    run_cmd: list[str],
    *,
    wake_backend: str = "shell",
    target: str | None = None,
    run_on: str | None = None,
    runner: Runner = _default_runner,
    timeout: int = 30,
    wake_available: bool | None = None,
    now: float | None = None,
) -> ScheduleResult:
    """Arm the next wakeup for an assignment, `interval_seconds` from now."""
    if wake_backend not in ("shell", *RESUME_BACKENDS):
        raise SchedulerError(f"unknown wake backend: {wake_backend!r}")
    if wake_backend == "wol" and not target:
        raise SchedulerError("the wol backend needs --wake-target <MAC address>")

    if wake_available is None:
        wake_available = shutil.which(WAKE_BIN) is not None
    base = now if now is not None else time.time()

    if not wake_available:
        if wake_backend in RESUME_BACKENDS:
            raise SchedulerError(
                f"the {wake_backend} backend needs the `wake` CLI on PATH "
                "(a systemd timer cannot wake a sleeping machine)"
            )
        job_id = _schedule_systemd_timer(
            assignment_id, max(interval_seconds, MIN_LEAD_SECONDS), run_cmd
        )
        return ScheduleResult(
            job_id=job_id,
            backend="systemd-timer",
            next_run_at=_isoformat(_floor_at(base, interval_seconds)),
        )

    resume_at = _floor_at(base, interval_seconds)
    resume_job_id: str | None = None
    if wake_backend in RESUME_BACKENDS:
        resume_job_id = _invoke_wake_add(
            resume_at,
            wake_backend,
            backend=wake_backend,
            target=target,
            task_id=resume_task_id_for(assignment_id),
            runner=runner,
            timeout=timeout,
        )
        run_at = resume_at + RESUME_GRACE_SECONDS
    else:
        run_at = resume_at

    job_id = _invoke_wake_add(
        run_at,
        " ".join(run_cmd),
        backend="shell",
        target=None,
        task_id=task_id_for(assignment_id),
        runner=runner,
        timeout=timeout,
        run_on=run_on,
    )
    return ScheduleResult(
        job_id=job_id,
        backend="wake",
        next_run_at=_isoformat(run_at),
        resume_job_id=resume_job_id,
    )


def cancel(
    job_id: str,
    backend: str,
    *,
    resume_job_id: str | None = None,
    runner: Runner = _default_runner,
    timeout: int = 30,
) -> None:
    if backend == "wake":
        _invoke_wake_cancel(job_id, runner=runner, timeout=timeout)
        if resume_job_id:
            _invoke_wake_cancel(resume_job_id, runner=runner, timeout=timeout)
    elif backend == "systemd-timer":
        _cancel_systemd_timer(job_id)
    else:
        raise SchedulerError(f"unknown scheduler backend: {backend!r}")
