"""Slot tests: when the machine wakes up, and when it powers itself off.

These two decisions are the ones nobody is watching at 08:00, so they get
argv-level assertions rather than "it did not raise". Every test drives the
real `slots.arm` against a fake wake runner and reads the command line it
built, which is the only thing wake actually sees.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from conftest import completed

from track import slots
from track.errors import SchedulerError
from track.store import Store


def at(*parts: int) -> float:
    """A local wall-clock instant, as an epoch.

    Naive on purpose and everywhere in this file: the code under test asks
    "what does the wall clock say", so a fixture pinned to UTC would test a
    different question and pass in every time zone but the one that matters.
    """
    return datetime(*parts).timestamp()  # noqa: DTZ001


# A Sunday at 12:00 local, so "earlier today" and "later today" are both
# unambiguous and neither sits near a DST boundary in any plausible zone.
NOON = at(2026, 9, 6, 12, 0, 0)


def _recording(stdout: str = "job-1") -> tuple[list[list[str]], Any]:
    calls: list[list[str]] = []

    def runner(cmd: list[str], *, timeout: int):
        calls.append(cmd)
        # The feature probe and `wake add` share one runner, so the probe's
        # answer has to come from the caller: a bare "job-1" contains no
        # "--every" and therefore reads as a wake that cannot recur.
        if cmd[1:2] == ["add"] and "--help" in cmd:
            return completed(stdout)
        return completed("job-1")

    return calls, runner


def _add_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[1:2] == ["add"] and "--help" not in c]


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def _assignment(store: Store, text: str, **kwargs: Any) -> str:
    return store.add_assignment(text, 86400, None, **kwargs).id


# -- parse_check_at ------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("08:00", "08:00"),
        ("8:00", "08:00"),
        ("0800", "08:00"),
        (" 8:00 ", "08:00"),
        ("00:00", "00:00"),
        ("23:59", "23:59"),
        ("7.30", "07:30"),
    ],
)
def test_check_times_normalise_to_one_spelling(given: str, expected: str) -> None:
    assert slots.parse_check_at(given) == expected


@pytest.mark.parametrize("given", ["25:00", "24:00", "abc", "", "8:60", "8", "08:0:0", "-1:00"])
def test_a_time_that_is_not_a_time_is_rejected(given: str) -> None:
    with pytest.raises(SchedulerError):
        slots.parse_check_at(given)


def test_the_rejection_says_what_was_wanted() -> None:
    with pytest.raises(SchedulerError, match="HH:MM"):
        slots.parse_check_at("half past eight")


def test_the_label_is_derived_from_the_time_so_re_arming_replaces() -> None:
    assert slots.slot_label("08:00") == "slot-0800"
    assert slots.slot_label("08:00") == slots.slot_label(slots.parse_check_at("8:00"))


# -- next_occurrence -----------------------------------------------------


def _local(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")  # noqa: DTZ006


def test_a_time_still_ahead_today_stays_today() -> None:
    assert _local(slots.next_occurrence("18:00", now=NOON)) == "2026-09-06 18:00"


def test_a_time_already_past_rolls_to_tomorrow() -> None:
    assert _local(slots.next_occurrence("08:00", now=NOON)) == "2026-09-07 08:00"


def test_midnight_is_a_time_like_any_other() -> None:
    assert _local(slots.next_occurrence("00:00", now=NOON)) == "2026-09-07 00:00"


def test_the_last_minute_of_the_day_is_reachable() -> None:
    assert _local(slots.next_occurrence("23:59", now=NOON)) == "2026-09-06 23:59"


def test_a_time_arriving_within_the_lead_floor_rolls_forward() -> None:
    """Not "in the past" -- "so close it may as well be".

    A wakeup armed for thirty seconds from now races the process arming it.
    The floor is what stops a slot re-armed at the end of its own run from
    firing again immediately.
    """
    just_before = at(2026, 9, 6, 7, 59, 30)
    assert _local(slots.next_occurrence("08:00", now=just_before)) == "2026-09-07 08:00"


# -- arm: what wake is actually told --------------------------------------


def test_a_slot_arms_one_task_for_all_its_members(store: Store) -> None:
    a = _assignment(store, "a laptop", check_at="08:00")
    b = _assignment(store, "a GPU", check_at="08:00")
    calls, runner = _recording()

    result = slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )

    assert len(_add_calls(calls)) == 1
    cmd = _add_calls(calls)[0]
    assert _flag(cmd, "--id") == "track-slot-0800"
    assert _flag(cmd, "--task") == "track run --slot 08:00"
    assert _flag(cmd, "--at") == str(slots.next_occurrence("08:00", now=NOON))
    # Both members point at the one job, because they really are one job.
    assert result is not None
    for assignment_id in (a, b):
        row = store.get_assignment(assignment_id)
        assert row is not None and row.job_id == result.job_id


def test_an_empty_slot_arms_nothing(store: Store) -> None:
    calls, runner = _recording()
    assert (
        slots.arm(
            store, "08:00", ["track", "run", "--slot", "08:00"],
            runner=runner, wake_available=True, supports_every=False, now=NOON,
        )
        is None
    )
    assert _add_calls(calls) == []


def test_the_slot_timeout_covers_every_member_in_sequence(store: Store) -> None:
    """wake's own default is 300s, which is under one cycle's ceiling."""
    from track.engine import WORST_CASE_RUN_SECONDS

    _assignment(store, "a laptop", check_at="08:00")
    _assignment(store, "a GPU", check_at="08:00")
    calls, runner = _recording()

    slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )

    assert int(_flag(_add_calls(calls)[0], "--timeout")) == 2 * WORST_CASE_RUN_SECONDS
    assert 2 * WORST_CASE_RUN_SECONDS > 300


# -- the poweroff rule ----------------------------------------------------


def test_a_slot_powers_off_when_every_member_asked_for_it(store: Store) -> None:
    _assignment(store, "a laptop", check_at="08:00", poweroff_after=True)
    _assignment(store, "a GPU", check_at="08:00", poweroff_after=True)
    calls, runner = _recording()

    result = slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )

    assert _flag(_add_calls(calls)[0], "--then") == "poweroff"
    assert result is not None and result.then == "poweroff"


def test_one_member_that_did_not_ask_keeps_the_machine_up(store: Store) -> None:
    """The rule, stated as a test so inverting it fails.

    An idle box is a cheaper mistake than a box that shuts down under a run
    somebody was still expecting a report from.
    """
    _assignment(store, "a laptop", check_at="08:00", poweroff_after=True)
    _assignment(store, "a GPU", check_at="08:00", poweroff_after=False)
    calls, runner = _recording()

    result = slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )

    assert "--then" not in _add_calls(calls)[0]
    assert result is not None and result.then is None


def test_a_member_leaving_can_turn_the_poweroff_back_on(store: Store) -> None:
    """The rule is re-evaluated on every arm, not decided once at add time."""
    _assignment(store, "a laptop", check_at="08:00", poweroff_after=True)
    holdout = _assignment(store, "a GPU", check_at="08:00", poweroff_after=False)
    calls, runner = _recording()

    slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )
    assert "--then" not in _add_calls(calls)[0]

    store.set_status(holdout, "paused")
    slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )
    assert _flag(_add_calls(calls)[1], "--then") == "poweroff"


# -- native recurrence, and the fallback ----------------------------------


def test_a_wake_with_every_gets_a_recurring_row(store: Store) -> None:
    _assignment(store, "a laptop", check_at="08:00")
    calls, runner = _recording()

    result = slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=True, now=NOON,
    )

    assert _flag(_add_calls(calls)[0], "--every") == "1d"
    assert result is not None and result.recurring


def test_a_wake_without_every_is_armed_one_shot(store: Store) -> None:
    _assignment(store, "a laptop", check_at="08:00")
    calls, runner = _recording()

    result = slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )

    assert "--every" not in _add_calls(calls)[0]
    assert result is not None and not result.recurring


def test_support_for_every_is_detected_from_the_help_text(store: Store) -> None:
    """Not from `wake --version`.

    The wake that has `--every` and the wake that does not both report
    0.1.0, so a version check answers confidently and wrongly. This is the
    branch most likely to rot: on the box this shipped from, detection
    currently answers "no".
    """
    _assignment(store, "a laptop", check_at="08:00")

    calls, runner = _recording(stdout="usage: wake add [--at AT] [--every EVERY] ...")
    slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, now=NOON,
    )
    assert ["wake", "add", "--help"] in calls
    assert _flag(_add_calls(calls)[0], "--every") == "1d"

    calls, runner = _recording(stdout="usage: wake add [--at AT] [--task TASK] ...")
    slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, now=NOON,
    )
    assert "--every" not in _add_calls(calls)[0]


def test_every_is_never_passed_to_an_rtcwake_resume_task(store: Store) -> None:
    """wake refuses `--every` on the rtcwake backend at add time.

    A recurring rtcwake pair would therefore fail on its resume half and
    leave a run task with nothing to wake the machine for it -- so the pair
    is re-armed after each run even though the run task alone could recur.
    """
    _assignment(store, "a laptop", check_at="08:00", wake_backend="rtcwake")
    calls, runner = _recording()

    result = slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=True, now=NOON,
    )

    resume, run = _add_calls(calls)
    assert _flag(resume, "--backend") == "rtcwake"
    assert "--every" not in resume
    assert _flag(run, "--every") == "1d"
    assert result is not None and not result.recurring


def test_a_wol_resume_task_does_recur(store: Store) -> None:
    """Only rtcwake is refused, so wol must not be punished for it."""
    _assignment(
        store, "a laptop", check_at="08:00", wake_backend="wol", wake_target="aa:bb:cc:dd:ee:ff"
    )
    calls, runner = _recording()

    result = slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=True, now=NOON,
    )

    resume, run = _add_calls(calls)
    assert _flag(resume, "--every") == "1d"
    assert _flag(run, "--every") == "1d"
    assert result is not None and result.recurring


def test_any_member_wanting_a_resume_decides_it_for_the_slot(store: Store) -> None:
    """Waking a machine that was already up costs nothing.

    Not waking one that was asleep costs the whole slot, so the resume wins
    over the plain shell member rather than being averaged with it.
    """
    _assignment(store, "a laptop", check_at="08:00")
    _assignment(
        store, "a GPU", check_at="08:00", wake_backend="wol", wake_target="aa:bb:cc:dd:ee:ff",
        wake_on="archserver",
    )
    calls, runner = _recording()

    slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
    )

    resume, run = _add_calls(calls)
    assert _flag(resume, "--backend") == "wol"
    assert _flag(resume, "--target") == "aa:bb:cc:dd:ee:ff"
    assert _flag(run, "--on") == "archserver"


def test_members_that_disagree_about_how_to_wake_are_reported(store: Store) -> None:
    _assignment(store, "a laptop", check_at="08:00", wake_backend="rtcwake")
    _assignment(
        store, "a GPU", check_at="08:00", wake_backend="wol", wake_target="aa:bb:cc:dd:ee:ff"
    )
    _calls, runner = _recording()
    warnings: list[str] = []

    slots.arm(
        store, "08:00", ["track", "run", "--slot", "08:00"],
        runner=runner, wake_available=True, supports_every=False, now=NOON,
        warn=warnings.append,
    )

    assert any("disagree" in w for w in warnings)


def test_a_paused_member_is_not_in_the_slot(store: Store) -> None:
    _assignment(store, "a laptop", check_at="08:00")
    paused = _assignment(store, "a GPU", check_at="08:00")
    store.set_status(paused, "paused")

    assert [a.id for a in store.slot_members("08:00")] != [paused]
    assert len(store.slot_members("08:00")) == 1


# -- drift, which is how a recurring slot survives a clock change ----------


def test_a_slot_firing_at_its_nominal_time_has_not_drifted() -> None:
    on_time = at(2026, 9, 6, 8, 0, 0)
    assert not slots.drifted("08:00", now=on_time)


def test_a_run_that_merely_started_late_is_not_drift() -> None:
    """wake polls, and a box coming back up takes a moment.

    Minutes of lateness are normal operation; re-anchoring on them would
    walk the slot forward a little every day.
    """
    late = at(2026, 9, 6, 8, 12, 0)
    assert not slots.drifted("08:00", now=late)


@pytest.mark.parametrize("hour", [7, 9])
def test_an_hour_out_is_drift(hour: int) -> None:
    """What a daylight-saving change does to an absolute-epoch recurrence."""
    shifted = at(2026, 10, 26, hour, 0, 0)
    assert slots.drifted("08:00", now=shifted)


def test_drift_is_measured_the_short_way_round_midnight() -> None:
    """A 00:15 slot firing at 23:45 is half an hour out, not 23.5 hours."""
    before_midnight = at(2026, 9, 6, 23, 45, 0)
    assert not slots.drifted("00:15", now=before_midnight)

    an_hour_before = at(2026, 9, 6, 23, 0, 0)
    assert slots.drifted("00:15", now=an_hour_before)
