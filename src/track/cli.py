"""Command-line interface for track.

track is an assignment-driven deal hunter: give it something to track, it
researches who sells that kind of thing cheap and where, schedules itself to
re-check on an interval (via the sibling `wake` tool, falling back to a
systemd --user timer), spins up Sonnet scouts each run, and posts what it
found to Discord via hotline-say.

Keeps stdout clean for real output (assignment ids, listings, summaries);
all progress and warnings go to stderr.

Exit codes: 0 success, 1 one or more operations failed, 2 usage error.
`track run` refines that, because it is the command a scheduler fires with
nobody watching and its exit status is the only signal that reaches anyone:

    0  a summary was posted and it contained at least one usable finding
    1  a summary was posted, honestly, but there was nothing usable in it
    2  usage error
    3  the summary could not be posted at all

Silence and success must not look alike at 08:00, so "ran but found nothing"
is deliberately not 0, and a report that never reached Discord is its own
code rather than being folded into a generic failure.

`track run --all-active` runs every active assignment and collapses their
outcomes into one code on the same principle: 3 if *any* summary failed to
post, else 0 if any assignment turned something up, else 1. One silent
assignment out of three is still a silent assignment.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

from . import __version__, scouts, slots
from .engine import (
    WORST_CASE_RUN_SECONDS,
    RunOutcome,
    run_assignment,
    run_command,
    slot_run_command,
)
from .errors import TrackError
from .models import Assignment, Finding, ListingStatus
from .scheduler import cancel as cancel_schedule
from .scheduler import schedule as schedule_wakeup
from .scheduler import wake_supports_every
from .scoring import source_stats
from .store import Store

_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# What an assignment falls back to when nobody -- not the user, not the
# advisor -- has said when to check it. Deliberately the same 6h that used to
# be the flag's default, so a track that cannot reach the advisor behaves
# exactly like the track that shipped before the advisor existed.
DEFAULT_INTERVAL = "6h"


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _parse_interval(value: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd]?)", value.strip())
    if not match or int(match.group(1)) <= 0:
        raise _UsageError(f"invalid --interval {value!r} (expected e.g. 6h, 30m, 86400)")
    return int(match.group(1)) * _INTERVAL_UNITS[match.group(2) or "s"]


# Every verb the parser accepts. Duplicated from `_build_parser` rather than
# read back out of argparse's internals, and pinned to it by a test, so the
# two drifting fails the suite instead of quietly mis-splitting an argument
# list.
SUBCOMMANDS = frozenset(
    {"add", "list", "show", "sources", "run", "web", "unschedule", "remove", "pause", "resume"}
)

# Of the flags accepted before a subcommand, only --db takes a value -- the
# one case where a bare "web" in the argument list is a value, not the verb.
_VALUE_FLAGS = {"--db"}


def _split_web_args(
    argv: list[str], commands: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Peel off everything after a literal `web` verb.

    `track web` hands its whole tail to a module this file knows nothing
    about, which argparse cannot express: REMAINDER still lets the parent
    parser claim a leading `--port`, and `nargs="*"` reorders the tail. So
    the split happens first, on the one verb that needs it.

    The scan stops at the first token that names a subcommand, so a value
    that happens to read like one -- `track add "..." --notify web` -- is not
    mistaken for the verb.
    """
    for i, token in enumerate(argv):
        if i and argv[i - 1] in _VALUE_FLAGS:
            continue
        if token in commands:
            return (argv[: i + 1], argv[i + 1 :]) if token == "web" else (argv, [])
    return argv, []


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="track", description="Assignment-driven deal hunter.")
    parser.add_argument("-v", "--verbose", action="store_true", help="print detailed progress")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", help="override the sqlite database path")

    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="track a new assignment")
    add_p.add_argument("text", help="what to track, e.g. 'a powerful but cheap laptop'")
    add_p.add_argument(
        "--interval",
        default=None,
        help="how often to re-check, as a period (e.g. 6h, 30m). Mutually exclusive "
        f"with --at. Either one, given explicitly, skips the advisor; given neither, "
        f"the advisor picks a daily time and {DEFAULT_INTERVAL} is the fallback if "
        "it cannot",
    )
    add_p.add_argument(
        "--at",
        dest="check_at",
        help="check daily at this local wall-clock time, e.g. 08:00. Mutually "
        "exclusive with --interval. Assignments sharing a time share one wakeup "
        "and run in sequence",
    )
    add_p.add_argument(
        "--then-poweroff",
        action="store_true",
        help="power the machine down once every assignment in this time slot has run "
        "(wake's --then poweroff); ignored unless every assignment in the slot asks for it",
    )
    add_p.add_argument(
        "--no-advise",
        action="store_true",
        help="do not spend a Sonnet call working out the best time to check; "
        f"use the {DEFAULT_INTERVAL} default instead",
    )
    add_p.add_argument("--max-price", type=float, default=None, help="price ceiling for reports")
    add_p.add_argument(
        "--market",
        default=os.environ.get("TRACK_MARKET"),
        help="where you are buying from, e.g. 'Serbia' -- a hard constraint on which "
        "sources count, not a preference (default: $TRACK_MARKET)",
    )
    add_p.add_argument(
        "--notify",
        help="hotline agent whose Discord channel gets the summary "
        "(required for scheduled runs, which have no session of their own)",
    )
    add_p.add_argument(
        "--wake-backend",
        choices=["shell", "rtcwake", "wol"],
        default="shell",
        help="shell runs on a machine that's already up; rtcwake/wol resume a sleeping one first",
    )
    add_p.add_argument("--wake-target", help="MAC address, for --wake-backend wol")
    add_p.add_argument(
        "--wake-on",
        help="wake origin name of the machine that should run the check; "
        "without it the wake server runs it, which is wrong when wol wakes a different box",
    )
    add_p.add_argument(
        "--no-schedule", action="store_true", help="do not schedule recurring wakeups"
    )

    list_p = sub.add_parser("list", help="list assignments")
    list_p.add_argument("--json", action="store_true", help="emit JSON")

    show_p = sub.add_parser("show", help="show an assignment's sources and best finds")
    show_p.add_argument("assignment_id")
    show_p.add_argument("--limit", type=int, default=5, help="how many top findings to show")
    show_p.add_argument("--json", action="store_true", help="emit JSON")

    sources_p = sub.add_parser("sources", help="who has actually been selling this cheap")
    sources_p.add_argument("assignment_id")
    sources_p.add_argument("--json", action="store_true", help="emit JSON")

    run_p = sub.add_parser("run", help="run one research cycle for an assignment now")
    run_p.add_argument(
        "assignment_id",
        nargs="?",
        help="which assignment to run; omit it and pass --all-active instead",
    )
    run_p.add_argument(
        "--slot",
        dest="slot",
        help="run every active assignment whose daily check time is this, in sequence "
        "-- what one shared wakeup fires",
    )
    run_p.add_argument(
        "--all-active",
        action="store_true",
        help="run every active assignment in turn -- what a scheduler with one "
        "wakeup for the whole database wants",
    )
    run_p.add_argument("--no-post", action="store_true", help="do not post the summary to Discord")
    run_p.add_argument("--force", action="store_true", help="run even if the assignment is paused")

    sub.add_parser(
        "web",
        help="serve the findings as a browsable site",
        description="Everything after `web` is passed through to the web module.",
    )
    # No flags declared on purpose: `track web` owns its own, so adding one
    # never means editing this file. They are split off before argparse sees
    # them (see `_split_web_args`) rather than declared as REMAINDER, which
    # leaks a leading `--port` to the parent parser as an unknown argument.

    unschedule_p = sub.add_parser(
        "unschedule",
        help="cancel the recurring wakeup but keep the assignment active "
        "(for when an external scheduler owns the timing)",
    )
    unschedule_p.add_argument("assignment_id")

    for name, help_text in [
        ("remove", "stop tracking an assignment and cancel its schedule"),
        ("pause", "stop scheduled runs without deleting history"),
        ("resume", "reschedule a paused assignment"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("assignment_id")

    return parser


@dataclass(frozen=True, slots=True)
class _Cadence:
    """When a new assignment should be checked, and on whose authority."""

    interval_seconds: int
    check_at: str | None = None
    rationale: str | None = None
    source: str | None = None  # "agent" | "user"


# What `interval_seconds` is set to for a daily assignment. It is not what
# schedules it -- the slot's wall-clock time is -- but the column is NOT NULL
# and something has to run the assignment if it is ever moved off a slot.
A_DAY = 86400


def _resolve_cadence(args: argparse.Namespace, log: Callable[[str], None]) -> _Cadence:
    """Decide a new assignment's cadence: the user's word, then the advisor's.

    Order matters and is the whole point. An explicit `--at` or `--interval`
    is a decision, not a hint, so the advisor is never consulted and never
    gets to overrule it. Only when the user has said nothing is a Sonnet call
    worth its second or two, and even then a failure is a warning rather than
    an error -- an assignment nobody can advise on is still an assignment
    worth tracking on the old flat interval.
    """
    if args.check_at:
        return _Cadence(A_DAY, check_at=slots.parse_check_at(args.check_at), source="user")
    if args.interval:
        return _Cadence(_parse_interval(args.interval))
    if args.no_advise or args.no_schedule:
        return _Cadence(_parse_interval(DEFAULT_INTERVAL))
    log("track: asking a Sonnet advisor what time of day is worth checking this...")
    try:
        advice = scouts.recommend_check_time(args.text, market=args.market)
    except TrackError as exc:
        log(
            f"track: warning: could not work out the best time to check ({exc}); "
            f"falling back to every {DEFAULT_INTERVAL}"
        )
        return _Cadence(_parse_interval(DEFAULT_INTERVAL))
    return _Cadence(
        A_DAY, check_at=advice.check_at, rationale=advice.rationale, source="agent"
    )


def _cadence_phrase(cadence: _Cadence) -> str:
    if cadence.check_at:
        who = "you asked" if cadence.source == "user" else "the advisor picked it"
        return f"daily at {cadence.check_at} ({who})"
    return f"every {cadence.interval_seconds}s"


def _arm_slot(store: Store, check_at: str, log: Callable[[str], None]) -> None:
    """(Re)arm the shared wakeup for one daily slot and report what it will do."""
    try:
        result = slots.arm(
            store, check_at, slot_run_command(store, check_at), warn=log
        )
    except TrackError as exc:
        log(f"track: warning: could not schedule the {check_at} slot: {exc}")
        return
    if result is None:
        log(f"the {check_at} slot has no active assignments left; its wakeup is cancelled")
        return
    members = store.slot_members(check_at)
    plural = "" if len(members) == 1 else "s"
    tail = ", then powers the machine off" if result.then == "poweroff" else ""
    recurs = "daily" if result.recurring else "once (re-armed after each run)"
    log(
        f"{check_at} slot: {len(members)} assignment{plural} via {result.backend} "
        f"(job {result.job_id}), {recurs}, next {result.next_run_at}{tail}"
    )


def _arm(store: Store, assignment: Assignment, log: Callable[[str], None]) -> None:
    if assignment.check_at:
        _arm_slot(store, assignment.check_at, log)
        return
    try:
        result = schedule_wakeup(
            assignment.id,
            assignment.interval_seconds,
            run_command(store, assignment.id),
            wake_backend=assignment.wake_backend or "shell",
            target=assignment.wake_target,
            run_on=assignment.wake_on,
            task_timeout=WORST_CASE_RUN_SECONDS,
        )
    except TrackError as exc:
        log(f"track: warning: could not schedule wakeups: {exc}")
        return
    store.set_schedule(
        assignment.id, result.job_id, result.backend, result.next_run_at, result.resume_job_id
    )
    log(f"scheduled via {result.backend} (job {result.job_id}) for {result.next_run_at}")


def _disarm(store: Store, assignment: Assignment, log: Callable[[str], None]) -> None:
    """Detach one assignment from whatever was going to run it.

    A slot member's wake task is shared, so cancelling it outright would take
    the other members down with it. The membership query is what decides:
    this must be called *after* the row has been paused or deleted, so the
    slot is re-armed for exactly the assignments that are left, and the task
    is only cancelled once none are.
    """
    if assignment.check_at:
        store.clear_schedule(assignment.id)
        if store.slot_members(assignment.check_at):
            _arm_slot(store, assignment.check_at, log)
            return
    if assignment.job_id and assignment.backend:
        try:
            cancel_schedule(
                assignment.job_id, assignment.backend, resume_job_id=assignment.resume_job_id
            )
        except TrackError as exc:
            log(f"track: warning: could not cancel schedule: {exc}")
    store.clear_schedule(assignment.id)


def _require(store: Store, assignment_id: str) -> Assignment:
    assignment = store.get_assignment(assignment_id)
    if assignment is None:
        raise TrackError(f"no such assignment {assignment_id!r}")
    return assignment


def _money(price: float | None, currency: str | None = None) -> str:
    if price is None:
        return "?"
    return f"{price:,.2f} {currency}" if currency else f"{price:,.2f}"


def _serve_web(store: Store, argv: list[str], log: Callable[[str], None]) -> int:
    """Hand off to the web module, which owns everything past `track web`.

    Imported here rather than at module scope so that a track installed
    without its web extra -- the shape on any machine that only runs the
    scheduled research -- is unaffected by the web half existing at all.
    `track` has no runtime dependencies and the unattended run depends on
    that staying true.

    Resolved through importlib rather than a `from .web import main`, because
    the module is genuinely optional and genuinely absent on those machines.
    A static import would also tie this file's type check to the state of a
    half maintained separately; a partially written `track.web` must not be
    able to turn `track run` red.

    The contract is one function:

        def main(argv: list[str], *, db_path: Path, log: Callable[[str], None]) -> int

    where `argv` is everything the user typed after `web`, untouched.
    """
    try:
        web = importlib.import_module("track.web")
        web_main = web.main
    except (ImportError, AttributeError) as exc:
        print(
            f"track: error: the web interface is not available ({exc}). "
            "Install it with: pip install -e '.[web]'",
            file=sys.stderr,
        )
        return 1
    return int(web_main(argv, db_path=Path(store.db_path), log=log))


def _provenance(finding: Finding, status: ListingStatus | None) -> str:
    """The age line under a finding in `track show`.

    Three different ages, deliberately not collapsed: how long we have known
    about the listing, how long the advert has been up, and how old the
    product is. A 2018 ThinkPad posted yesterday is a new advert for an old
    machine, and none of the three implies the others.
    """
    parts = []
    if status is not None:
        parts.append(f"first seen {status.first_seen_at[:10]}")
    if finding.listing_age_days is not None:
        parts.append(f"listed {int(finding.listing_age_days)}d ago")
    elif finding.listing_posted_at:
        parts.append(f"listed {finding.listing_posted_at}")
    if finding.product_year:
        parts.append(f"{finding.product_year} model")
    if finding.condition:
        parts.append(finding.condition)
    if status is not None:
        parts.append(f"seen in {status.times_seen} run(s)")
    return " · ".join(parts)


def _run_exit_code(outcome: RunOutcome, *, posting: bool, log: Callable[[str], None]) -> int:
    if not posting:
        return 0 if outcome.usable else 1
    if not outcome.posted:
        print("track: error: the summary could not be posted", file=sys.stderr)
        return 3
    if not outcome.usable:
        log("track: nothing usable found this run (summary posted anyway)")
        return 1
    return 0


def _run_all_active(store: Store, args: argparse.Namespace, log: Callable[[str], None]) -> int:
    """Run every active assignment, then report the worst thing that happened.

    A scheduler that owns one wakeup for the whole database needs this to be
    one command, and it needs one exit code out of several runs. An unposted
    summary dominates a merely empty one: it is the outcome nobody hears
    about otherwise, and one silent assignment out of three is still a silent
    assignment. A failing assignment never stops the ones after it.
    """
    active = [a for a in store.list_assignments() if a.status == "active"]
    if not active:
        log("track: no active assignments")
        return 1
    return _run_sequence(store, active, args, log)


def _run_sequence(
    store: Store,
    assignments: list[Assignment],
    args: argparse.Namespace,
    log: Callable[[str], None],
) -> int:
    """Run a list of assignments in order and collapse them into one code.

    Shared by --all-active and --slot because the collapsing rule is the same
    one either way, and a slot that scored its runs differently from the
    whole-database sweep would mean two answers to "did this morning work".
    """
    if not assignments:
        log("track: nothing to run")
        return 1
    worst = 1
    any_usable = False
    for assignment in assignments:
        log(f"track: running {assignment.id} ({assignment.text[:60]})")
        try:
            outcome = run_assignment(store, assignment, warn=log, post=not args.no_post)
        except TrackError as exc:
            print(f"track: error: {assignment.id} failed: {exc}", file=sys.stderr)
            worst = max(worst, 3)
            continue
        print(outcome.summary)
        code = _run_exit_code(outcome, posting=not args.no_post, log=log)
        if code == 3:
            worst = 3
        if outcome.usable:
            any_usable = True
    if worst == 3:
        return 3
    return 0 if any_usable else 1


def _cadence_line(assignment: Assignment) -> str:
    if assignment.check_at:
        return f"daily at {assignment.check_at}"
    return f"every {assignment.interval_seconds}s"


def _schedule_notes(store: Store, assignment: Assignment) -> list[str]:
    """The why behind the schedule, for `track show`.

    A check time with no reasoning attached is a number nobody can audit, so
    the advisor's own words are printed rather than just the hour it chose --
    and who chose it, because "the model picked 07:00" and "you asked for
    07:00" are different claims about how much to trust it.
    """
    notes: list[str] = []
    if assignment.check_at_rationale:
        who = {"agent": "advisor", "user": "you"}.get(
            assignment.check_at_source or "", "unknown"
        )
        when = assignment.check_at or "(no longer scheduled to a time)"
        notes.append(f"why {when}: {assignment.check_at_rationale} [{who}]")
    elif assignment.check_at and assignment.check_at_source == "user":
        notes.append(f"why {assignment.check_at}: you asked for it")
    if assignment.check_at:
        members = store.slot_members(assignment.check_at)
        others = [m.id for m in members if m.id != assignment.id]
        shared = f", shared with {', '.join(others)}" if others else ""
        if assignment.poweroff_after:
            tail = (
                "powers the machine off afterwards"
                if all(m.poweroff_after for m in members)
                else "asked to power off, but another assignment in this slot did not, "
                "so the machine stays up"
            )
        else:
            tail = "leaves the machine up"
        notes.append(f"{assignment.check_at} slot: {len(members)} assignment(s){shared}; {tail}")
    return notes


def _run_slot(store: Store, args: argparse.Namespace, log: Callable[[str], None]) -> int:
    """Run one daily slot: every assignment due at this time, in sequence.

    This is what a slot's single wake task fires, and the sequence is the
    point -- each cycle already fans five Sonnet scouts out on a 6-core box,
    and if the slot is going to power the machine off it must do so after the
    last assignment rather than after whichever finished first.

    The slot is re-armed here only when wake cannot recur on its own -- on a
    wake with `--every`, the row is already scheduled for tomorrow and
    re-arming it mid-fire would rewrite the task that is currently running --
    or when the slot has drifted off its wall-clock time, which is how a
    recurring row survives a daylight-saving change (see slots.drifted).
    """
    check_at = slots.parse_check_at(args.slot)
    code = _run_sequence(store, store.slot_members(check_at), args, log)
    if not scheduler_recurs(store, check_at):
        _arm_slot(store, check_at, log)
    elif slots.drifted(check_at):
        log(f"track: the {check_at} slot fired well off its time; re-anchoring to local time")
        _arm_slot(store, check_at, log)
    return code


def scheduler_recurs(store: Store, check_at: str) -> bool:
    """Whether the slot's task will fire again without track re-arming it."""
    if not wake_supports_every():
        return False
    members = store.slot_members(check_at)
    # An rtcwake slot's resume half cannot recur (wake refuses --every on it),
    # so the pair has to be re-armed even though the run task would recur.
    return not any((m.wake_backend or "shell") == "rtcwake" for m in members)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    tokens = list(sys.argv[1:] if argv is None else argv)
    tokens, web_args = _split_web_args(tokens, SUBCOMMANDS)
    try:
        args = parser.parse_args(tokens)
    except _UsageError as exc:
        print(f"track: error: {exc}", file=sys.stderr)
        return 2

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    def vlog(message: str) -> None:
        if args.verbose and not args.quiet:
            print(message, file=sys.stderr)

    if args.command == "run":
        selectors = [bool(args.assignment_id), bool(args.all_active), bool(args.slot)]
        if sum(selectors) != 1:
            print(
                "track: error: run takes exactly one of an assignment id, --all-active "
                "or --slot HH:MM",
                file=sys.stderr,
            )
            return 2

    interval_seconds = 0
    cadence = _Cadence(0)
    if args.command == "add":
        if args.interval and args.check_at:
            print(
                "track: error: --interval and --at are alternatives "
                "(--at is a daily wall-clock time, --interval a period)",
                file=sys.stderr,
            )
            return 2
        # Every cheap usage check runs before the advisor does. Resolving the
        # cadence can spend a Sonnet call, and spending one to then reject the
        # command line for a missing MAC address is pure waste.
        if args.wake_backend == "wol" and not args.wake_target:
            print(
                "track: error: --wake-backend wol needs --wake-target <MAC address>",
                file=sys.stderr,
            )
            return 2
        try:
            cadence = _resolve_cadence(args, log)
        except TrackError as exc:
            print(f"track: error: {exc}", file=sys.stderr)
            return 2
        except _UsageError as exc:
            print(f"track: error: {exc}", file=sys.stderr)
            return 2
        interval_seconds = cadence.interval_seconds
        if not args.notify and not args.no_schedule:
            log(
                "track: warning: no --notify agent; scheduled runs have no Claude session, "
                "so their summary will have nowhere to go "
                "(set --notify <agent> or TRACK_HOTLINE_AGENT)"
            )

    try:
        with Store(Path(args.db) if args.db else None) as store:
            if args.command == "add":
                assignment = store.add_assignment(
                    args.text,
                    interval_seconds,
                    args.max_price,
                    market=args.market,
                    notify_agent=args.notify,
                    wake_backend=args.wake_backend,
                    wake_target=args.wake_target,
                    wake_on=args.wake_on,
                    check_at=cadence.check_at,
                    check_at_rationale=cadence.rationale,
                    check_at_source=cadence.source,
                    poweroff_after=args.then_poweroff,
                )
                where = f" in {assignment.market}" if assignment.market else ""
                when = (
                    "on demand only (not scheduled)"
                    if args.no_schedule
                    else _cadence_phrase(cadence)
                )
                log(f"tracking {assignment.id}: {assignment.text!r}{where} {when}")
                if cadence.rationale:
                    log(f"  why {cadence.check_at}: {cadence.rationale}")
                if not assignment.market:
                    log(
                        "track: warning: no --market set, so scouts will return whatever "
                        "the open web surfaces -- usually US sources (set --market or "
                        "TRACK_MARKET)"
                    )
                if not args.no_schedule:
                    _arm(store, assignment, vlog if args.verbose else log)
                print(assignment.id)
                return 0

            if args.command == "list":
                assignments = store.list_assignments()
                if args.json:
                    print(json.dumps([asdict(a) for a in assignments], indent=2))
                    return 0
                if not assignments:
                    log("no assignments tracked yet.")
                    return 0
                for a in assignments:
                    print(
                        f"{a.id}\t{a.status}\t{a.text}\t"
                        f"(runs: {a.runs_count}, last: {a.last_run_at or 'never'})"
                    )
                return 0

            if args.command == "show":
                assignment = _require(store, args.assignment_id)
                sources = store.list_sources(assignment.id)
                best = store.best_findings(assignment.id, args.limit)
                retired = store.retired_listings(assignment.id)
                if args.json:
                    print(
                        json.dumps(
                            {
                                "assignment": asdict(assignment),
                                "sources": [asdict(s) for s in sources],
                                "best": [asdict(f) for f in best],
                                "retired": [asdict(s) for s in retired],
                                "total_cost_usd": store.total_cost(assignment.id),
                            },
                            indent=2,
                        )
                    )
                    return 0
                print(f"{assignment.id}: {assignment.text} [{assignment.status}]")
                print(
                    f"  {_cadence_line(assignment)} via "
                    f"{assignment.backend or 'no schedule'}"
                    f" · {assignment.runs_count} runs"
                    f" · ~${store.total_cost(assignment.id):.2f} of model usage"
                )
                for line in _schedule_notes(store, assignment):
                    print(f"  {line}")
                print(f"sources ({len(sources)}):")
                for s in sources:
                    print(f"  - {s.name} (seen {s.times_seen}x) {s.url or ''}".rstrip())
                print(f"best finds ({len(best)}):")
                for f in best:
                    score = f"{f.score:.2f}" if f.score is not None else "?"
                    basis = f" {f.score_basis}" if f.score_basis else ""
                    print(
                        f"  - {f.title} — {_money(f.price, f.currency)} @ {f.source} "
                        f"(score {score}{basis}) {f.url or ''}".rstrip()
                    )
                    if f.reference_price:
                        print(
                            f"      vs {_money(f.reference_price, f.currency)} from "
                            f"{f.reference_n} comparable(s)"
                        )
                    if f.rationale:
                        print(f"      {f.rationale}")
                    provenance = _provenance(f, store.listing_status(assignment.id, f.dedup_key))
                    if provenance:
                        print(f"      {provenance}")
                if retired:
                    print(f"retired ({len(retired)}):")
                    for status in retired[:args.limit]:
                        note = f" — {status.retired_note}" if status.retired_note else ""
                        print(f"  - [{status.retired_reason}] {status.dedup_key}{note}")
                return 0

            if args.command == "sources":
                assignment = _require(store, args.assignment_id)
                stats = source_stats(store.latest_findings(assignment.id))
                if args.json:
                    print(json.dumps([asdict(s) for s in stats], indent=2))
                    return 0
                if not stats:
                    log("no findings yet -- run the assignment first.")
                    return 0
                print(f"{assignment.id}: {assignment.text}")
                for stat in stats:
                    label = f"{stat.name} ({stat.currency})" if stat.currency else stat.name
                    print(
                        f"  {label}: {stat.listings} listings, {stat.priced} priced "
                        f"({stat.price_rate:.0%}), from {_money(stat.cheapest, stat.currency)}, "
                        f"median {_money(stat.median, stat.currency)}"
                    )
                return 0

            if args.command == "web":
                return _serve_web(store, web_args, log)

            if args.command == "run":
                if args.slot:
                    return _run_slot(store, args, log)
                if args.all_active:
                    return _run_all_active(store, args, log)
                assignment = _require(store, args.assignment_id)
                if assignment.status != "active" and not args.force:
                    print(
                        f"track: error: {assignment.id} is {assignment.status} "
                        "(use --force to run anyway)",
                        file=sys.stderr,
                    )
                    return 1
                outcome = run_assignment(store, assignment, warn=log, post=not args.no_post)
                print(outcome.summary)
                return _run_exit_code(outcome, posting=not args.no_post, log=log)

            # The row is changed *before* _disarm in all three, not after.
            # A slot's wake task is shared, and _disarm re-arms it for
            # whoever is still in the slot -- so the assignment being taken
            # out has to have stopped being a member by then, or the slot is
            # rebuilt around it and nothing actually changes.
            if args.command == "unschedule":
                assignment = _require(store, args.assignment_id)
                if assignment.check_at:
                    # Leaving the slot is what "an external scheduler owns
                    # the timing" means for a daily assignment. The advisor's
                    # reasoning is kept, so `track show` can still say what
                    # time was suggested and why.
                    store.set_check_at(
                        assignment.id,
                        None,
                        rationale=assignment.check_at_rationale,
                        source=assignment.check_at_source,
                    )
                _disarm(store, assignment, log)
                log(f"unscheduled {assignment.id}; it stays active but will not self-run")
                return 0

            if args.command == "remove":
                assignment = _require(store, args.assignment_id)
                store.remove_assignment(assignment.id)
                _disarm(store, assignment, log)
                log(f"removed {assignment.id}")
                return 0

            if args.command == "pause":
                assignment = _require(store, args.assignment_id)
                store.set_status(assignment.id, "paused")
                _disarm(store, assignment, log)
                log(f"paused {assignment.id}")
                return 0

            if args.command == "resume":
                assignment = _require(store, args.assignment_id)
                store.set_status(assignment.id, "active")
                _arm(store, _require(store, assignment.id), log)
                log(f"resumed {assignment.id}")
                return 0

            return 2
    except TrackError as exc:
        print(f"track: error: {exc}", file=sys.stderr)
        return 1
