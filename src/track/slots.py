"""Daily check slots: several assignments, one wakeup, one shutdown.

The unit that gets scheduled is a *time*, not an assignment.

Why it has to be. An assignment that powers the machine off when it finishes
cannot own its own wakeup once there is more than one of them: two
assignments due at 08:00 arm two tasks, the first to finish pulls the plug,
and the second never runs -- and, worse, never reports that it did not.
Giving each assignment its own private time would only narrow the race, not
remove it, because a research cycle's duration is whatever five Sonnet scouts
take. So assignments that share a wall-clock time share one wake task, which
runs them in sequence (`track run --slot HH:MM`) and shuts down once, after
the last one. That is also why `~/.local/bin/track-run-all` exists: runs have
to be sequential on a 6-core box, and this makes that property structural
instead of a convention in a shell script.

Powering off per slot rather than once at the end of the day is deliberate:
with a resume backend, each slot wakes the machine itself, so a 19:00 slot is
not harmed by the 08:00 slot having shut the box down. On a machine that is
simply always up, a slot with no resume backend that powers off is a slot
that ends the day early -- which is why the opt-in is per assignment and the
slot only powers off when *every* member asked for it. One member that did
not is enough to keep the machine up: the cost of that is an idle box, and
the cost of the opposite is somebody's session dying under them.

`interval_seconds` assignments are untouched by any of this. They keep their
own per-assignment task; a slot is what you get by naming a time instead.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from . import scheduler
from .errors import SchedulerError
from .models import Assignment
from .scheduler import RESUME_BACKENDS, ScheduleResult
from .store import Store

_CLOCK_RE = re.compile(r"^\s*(\d{1,2})[:.]?([0-5]\d)\s*$")

# What track asks wake to repeat on. Only used when the wake on PATH supports
# `--every`; otherwise the slot re-arms itself after each run, as before.
DAILY = "1d"


def parse_check_at(value: str) -> str:
    """Normalise a wall-clock time to "HH:MM", or raise.

    Accepts "8:00", "08:00" and "0800" because all three are things a person
    types, and stores exactly one spelling because the slot label, the task
    id and the `--slot` argument all have to agree.
    """
    match = _CLOCK_RE.match(value)
    if not match:
        raise SchedulerError(f"invalid check time {value!r} (expected HH:MM, e.g. 08:00)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23:
        raise SchedulerError(f"invalid check time {value!r} (hour must be 00-23)")
    return f"{hour:02d}:{minute:02d}"


def slot_label(check_at: str) -> str:
    """The scheduler label for a slot: "slot-0800" -> task `track-slot-0800`.

    Derived from the time rather than generated, so re-arming a slot replaces
    its task instead of adding a second one, and so a slot whose membership
    changed between runs is still the same task.
    """
    return f"slot-{check_at.replace(':', '')}"


def next_occurrence(check_at: str, *, now: float | None = None) -> int:
    """Epoch seconds of the next local HH:MM, at least MIN_LEAD_SECONDS away.

    Computed on a naive local datetime on purpose. Adding a day to an
    *aware* datetime adds 24 hours of elapsed time, which lands an hour off
    the intended wall clock on the two days a year the offset changes; adding
    it to a naive one keeps the wall clock and lets the platform resolve the
    offset at the end. The two ambiguous hours around a DST change still
    resolve to whichever instant Python picks -- a slot can therefore run an
    hour early or late twice a year, which is not worth a tz database to fix.
    """
    hour, minute = (int(part) for part in check_at.split(":"))
    stamp = now if now is not None else time.time()
    base = datetime.fromtimestamp(stamp)  # noqa: DTZ006 -- naive is the point, see above
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= stamp + scheduler.MIN_LEAD_SECONDS:
        candidate += timedelta(days=1)
    return int(candidate.timestamp())


def _resume_config(
    members: list[Assignment], warn: Callable[[str], None]
) -> tuple[str, str | None, str | None]:
    """The wake backend one slot fires with, out of its members' settings.

    A slot has one wakeup, so it has one answer here. Any member asking to be
    resumed decides it for the slot -- waking a machine that was already up
    costs nothing, while not waking one that was asleep costs the whole slot.
    Members that disagree about *how* are a misconfiguration rather than
    something to average, so the first is used and the conflict is reported.
    """
    resumers = [m for m in members if (m.wake_backend or "shell") in RESUME_BACKENDS]
    if not resumers:
        return "shell", None, next((m.wake_on for m in members if m.wake_on), None)
    chosen = resumers[0]
    distinct = {(m.wake_backend, m.wake_target, m.wake_on) for m in resumers}
    if len(distinct) > 1:
        warn(
            f"assignments in the {members[0].check_at} slot disagree on how to wake the "
            f"machine; using {chosen.wake_backend} from {chosen.id}"
        )
    return chosen.wake_backend or "shell", chosen.wake_target, chosen.wake_on


def arm(
    store: Store,
    check_at: str,
    run_cmd: list[str],
    *,
    supports_every: bool | None = None,
    runner: scheduler.Runner = scheduler._default_runner,
    wake_available: bool | None = None,
    warn: Callable[[str], None] = lambda _m: None,
    now: float | None = None,
) -> ScheduleResult | None:
    """(Re)arm the wake task for one slot and point its members at it.

    Returns None when the slot has no active members left, having cancelled
    whatever task they were sharing. Every member's row records the same
    job id, which is the honest thing: they really are one job.
    """
    members = store.slot_members(check_at)
    if not members:
        stale = None
        for assignment in store.list_assignments():
            if assignment.check_at == check_at and assignment.job_id:
                stale = assignment
                break
        if stale is not None and stale.backend:
            try:
                scheduler.cancel(
                    stale.job_id or "",
                    stale.backend,
                    resume_job_id=stale.resume_job_id,
                    runner=runner,
                )
            except SchedulerError as exc:
                warn(f"could not cancel the {check_at} slot: {exc}")
        return None

    if supports_every is None:
        supports_every = scheduler.wake_supports_every(runner=runner)
    wake_backend, target, run_on = _resume_config(members, warn)

    # Every member has to have opted in. See the module docstring: an idle
    # box is a cheaper mistake than a box that shuts down under a run that
    # was still going to happen.
    poweroff = all(m.poweroff_after for m in members)

    result = scheduler.schedule(
        members[0].id,
        members[0].interval_seconds,
        run_cmd,
        wake_backend=wake_backend,
        target=target,
        run_on=run_on,
        runner=runner,
        wake_available=wake_available,
        label=slot_label(check_at),
        at_epoch=next_occurrence(check_at, now=now),
        at_clock=check_at,
        every=DAILY if supports_every else None,
        then="poweroff" if poweroff else None,
    )
    for member in members:
        store.set_schedule(
            member.id, result.job_id, result.backend, result.next_run_at, result.resume_job_id
        )
    return result
