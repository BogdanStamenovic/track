"""Command-line interface for track.

track is an assignment-driven deal hunter: give it something to track, it
researches who sells that kind of thing cheap and where, schedules itself
to re-check on an interval (via the sibling `wake` tool, falling back to a
systemd --user timer), spins up Sonnet scouts each run, and posts what it
found to Discord via hotline-say.

Keeps stdout clean for real output (assignment ids, listings, summaries);
all progress and warnings go to stderr. Exit codes: 0 success, 1 one or
more operations failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

from . import __version__
from .engine import run_assignment
from .errors import TrackError
from .scheduler import cancel as cancel_schedule
from .scheduler import schedule as schedule_wakeup
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
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    return amount * _INTERVAL_UNITS[unit]


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
    add_p.add_argument("--max-price", type=float, default=None, help="optional price ceiling")
    add_p.add_argument(
        "--no-schedule", action="store_true", help="do not schedule recurring wakeups"
    )

    sub.add_parser("list", help="list assignments")

    show_p = sub.add_parser("show", help="show an assignment's sources and best finds")
    show_p.add_argument("assignment_id")
    show_p.add_argument("--limit", type=int, default=5, help="how many top findings to show")

    run_p = sub.add_parser("run", help="run one research cycle for an assignment now")
    run_p.add_argument("assignment_id")
    run_p.add_argument("--no-post", action="store_true", help="do not post the summary to Discord")

    remove_p = sub.add_parser("remove", help="stop tracking an assignment and cancel its schedule")
    remove_p.add_argument("assignment_id")

    pause_p = sub.add_parser("pause", help="stop scheduled runs without deleting history")
    pause_p.add_argument("assignment_id")

    resume_p = sub.add_parser("resume", help="reschedule a paused assignment")
    resume_p.add_argument("assignment_id")

    return parser


def _run_command(subcommand: list[str]) -> list[str]:
    """The absolute command a scheduler should run for a `track` subcommand.

    A scheduled wakeup fires from `wake` or systemd, neither of which shares
    this process's PATH -- falling back to a bare "track" would silently
    fail at fire time. `sys.argv[0]` is whatever path was actually used to
    invoke this process, so resolving it gives a path that works regardless
    of how track is installed.
    """
    track_bin = shutil.which("track") or str(Path(sys.argv[0]).resolve())
    return [track_bin, *subcommand]


def _cancel_if_scheduled(
    store: Store,
    assignment_id: str,
    job_id: str | None,
    backend: str | None,
    log: Callable[[str], None],
) -> None:
    if job_id and backend:
        try:
            cancel_schedule(job_id, backend)
        except TrackError as exc:
            log(f"track: warning: could not cancel schedule: {exc}")
    store.clear_schedule(assignment_id)


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

    db_path = Path(args.db) if args.db else None

    if args.command == "add":
        try:
            interval_seconds = _parse_interval(args.interval)
        except _UsageError as exc:
            print(f"track: error: {exc}", file=sys.stderr)
            return 2

    try:
        with Store(db_path) as store:
            if args.command == "add":
                new_assignment = store.add_assignment(args.text, interval_seconds, args.max_price)
                log(f"tracking {new_assignment.id}: {new_assignment.text!r} every {args.interval}")
                if not args.no_schedule:
                    try:
                        result = schedule_wakeup(
                            new_assignment.id,
                            interval_seconds,
                            _run_command(["run", new_assignment.id]),
                        )
                        store.set_schedule(
                            new_assignment.id, result.job_id, result.backend, result.next_run_at
                        )
                        vlog(f"scheduled via {result.backend} (job {result.job_id})")
                    except TrackError as exc:
                        log(f"track: warning: could not schedule wakeups: {exc}")
                print(new_assignment.id)
                return 0

            if args.command == "list":
                assignments = store.list_assignments()
                if not assignments:
                    log("no assignments tracked yet.")
                    return 0
                for a in assignments:
                    print(f"{a.id}\t{a.status}\t{a.text}\t(last run: {a.last_run_at or 'never'})")
                return 0

            if args.command == "show":
                assignment = store.get_assignment(args.assignment_id)
                if assignment is None:
                    print(f"track: error: no such assignment {args.assignment_id!r}", file=sys.stderr)
                    return 1
                print(f"{assignment.id}: {assignment.text} [{assignment.status}]")
                sources = store.list_sources(assignment.id)
                print(f"sources ({len(sources)}):")
                for s in sources:
                    print(f"  - {s.name} (seen {s.times_seen}x) {s.url or ''}")
                best = store.best_findings(assignment.id, args.limit)
                print(f"best finds ({len(best)}):")
                for f in best:
                    price = f"{f.price:.2f} {f.currency}" if f.price is not None else "?"
                    print(f"  - {f.title} — {price} @ {f.source} (score {f.score or 0:.2f}) {f.url or ''}")
                return 0

            if args.command == "run":
                assignment = store.get_assignment(args.assignment_id)
                if assignment is None:
                    print(f"track: error: no such assignment {args.assignment_id!r}", file=sys.stderr)
                    return 1
                _findings, summary = run_assignment(
                    store, assignment, warn=log, post=not args.no_post
                )
                print(summary)
                return 0

            if args.command == "remove":
                assignment = store.get_assignment(args.assignment_id)
                if assignment is None:
                    print(f"track: error: no such assignment {args.assignment_id!r}", file=sys.stderr)
                    return 1
                _cancel_if_scheduled(store, assignment.id, assignment.job_id, assignment.backend, log)
                store.remove_assignment(assignment.id)
                log(f"removed {assignment.id}")
                return 0

            if args.command == "pause":
                assignment = store.get_assignment(args.assignment_id)
                if assignment is None:
                    print(f"track: error: no such assignment {args.assignment_id!r}", file=sys.stderr)
                    return 1
                _cancel_if_scheduled(store, assignment.id, assignment.job_id, assignment.backend, log)
                store.set_status(assignment.id, "paused")
                log(f"paused {assignment.id}")
                return 0

            if args.command == "resume":
                assignment = store.get_assignment(args.assignment_id)
                if assignment is None:
                    print(f"track: error: no such assignment {args.assignment_id!r}", file=sys.stderr)
                    return 1
                store.set_status(assignment.id, "active")
                try:
                    result = schedule_wakeup(
                        assignment.id,
                        assignment.interval_seconds,
                        _run_command(["run", assignment.id]),
                    )
                    store.set_schedule(
                        assignment.id, result.job_id, result.backend, result.next_run_at
                    )
                    log(f"resumed {assignment.id} via {result.backend}")
                except TrackError as exc:
                    log(f"track: warning: could not schedule wakeups: {exc}")
                return 0

            return 2
    except TrackError as exc:
        print(f"track: error: {exc}", file=sys.stderr)
        return 1
