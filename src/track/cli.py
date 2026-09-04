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
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from . import __version__
from .engine import RunOutcome, run_assignment, run_command
from .errors import TrackError
from .models import Assignment
from .scheduler import cancel as cancel_schedule
from .scheduler import schedule as schedule_wakeup
from .scoring import source_stats
from .store import Store

_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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
        "--interval", default="6h", help="how often to re-check (e.g. 6h, 30m); default 6h"
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
        "--all-active",
        action="store_true",
        help="run every active assignment in turn -- what a scheduler with one "
        "wakeup for the whole database wants",
    )
    run_p.add_argument("--no-post", action="store_true", help="do not post the summary to Discord")
    run_p.add_argument("--force", action="store_true", help="run even if the assignment is paused")

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


def _arm(store: Store, assignment: Assignment, log: Callable[[str], None]) -> None:
    try:
        result = schedule_wakeup(
            assignment.id,
            assignment.interval_seconds,
            run_command(store, assignment.id),
            wake_backend=assignment.wake_backend or "shell",
            target=assignment.wake_target,
            run_on=assignment.wake_on,
        )
    except TrackError as exc:
        log(f"track: warning: could not schedule wakeups: {exc}")
        return
    store.set_schedule(
        assignment.id, result.job_id, result.backend, result.next_run_at, result.resume_job_id
    )
    log(f"scheduled via {result.backend} (job {result.job_id}) for {result.next_run_at}")


def _disarm(store: Store, assignment: Assignment, log: Callable[[str], None]) -> None:
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
    worst = 1
    any_usable = False
    for assignment in active:
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"track: error: {exc}", file=sys.stderr)
        return 2

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    def vlog(message: str) -> None:
        if args.verbose and not args.quiet:
            print(message, file=sys.stderr)

    if args.command == "run" and bool(args.assignment_id) == bool(args.all_active):
        print("track: error: run takes either an assignment id or --all-active", file=sys.stderr)
        return 2

    interval_seconds = 0
    if args.command == "add":
        try:
            interval_seconds = _parse_interval(args.interval)
        except _UsageError as exc:
            print(f"track: error: {exc}", file=sys.stderr)
            return 2
        if args.wake_backend == "wol" and not args.wake_target:
            print(
                "track: error: --wake-backend wol needs --wake-target <MAC address>",
                file=sys.stderr,
            )
            return 2
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
                )
                where = f" in {assignment.market}" if assignment.market else ""
                cadence = (
                    "on demand only (not scheduled)"
                    if args.no_schedule
                    else f"every {args.interval}"
                )
                log(f"tracking {assignment.id}: {assignment.text!r}{where} {cadence}")
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
                if args.json:
                    print(
                        json.dumps(
                            {
                                "assignment": asdict(assignment),
                                "sources": [asdict(s) for s in sources],
                                "best": [asdict(f) for f in best],
                                "total_cost_usd": store.total_cost(assignment.id),
                            },
                            indent=2,
                        )
                    )
                    return 0
                print(f"{assignment.id}: {assignment.text} [{assignment.status}]")
                print(
                    f"  every {assignment.interval_seconds}s via "
                    f"{assignment.backend or 'no schedule'}"
                    f" · {assignment.runs_count} runs"
                    f" · ~${store.total_cost(assignment.id):.2f} of model usage"
                )
                print(f"sources ({len(sources)}):")
                for s in sources:
                    print(f"  - {s.name} (seen {s.times_seen}x) {s.url or ''}".rstrip())
                print(f"best finds ({len(best)}):")
                for f in best:
                    score = f"{f.score:.2f}" if f.score is not None else "?"
                    print(
                        f"  - {f.title} — {_money(f.price, f.currency)} @ {f.source} "
                        f"(score {score}) {f.url or ''}".rstrip()
                    )
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

            if args.command == "run":
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

            if args.command == "unschedule":
                assignment = _require(store, args.assignment_id)
                _disarm(store, assignment, log)
                log(f"unscheduled {assignment.id}; it stays active but will not self-run")
                return 0

            if args.command == "remove":
                assignment = _require(store, args.assignment_id)
                _disarm(store, assignment, log)
                store.remove_assignment(assignment.id)
                log(f"removed {assignment.id}")
                return 0

            if args.command == "pause":
                assignment = _require(store, args.assignment_id)
                _disarm(store, assignment, log)
                store.set_status(assignment.id, "paused")
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
