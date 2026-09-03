"""Command-line interface for track.

track is an assignment-driven deal hunter: give it something to track, it
researches who sells that kind of thing cheap and where, schedules itself to
re-check on an interval (via the sibling `wake` tool, falling back to a
systemd --user timer), spins up Sonnet scouts each run, and posts what it
found to Discord via hotline-say.

Keeps stdout clean for real output (assignment ids, listings, summaries);
all progress and warnings go to stderr. Exit codes: 0 success, 1 one or
more operations failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from . import __version__
from .engine import run_assignment, run_command
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
    run_p.add_argument("assignment_id")
    run_p.add_argument("--no-post", action="store_true", help="do not post the summary to Discord")
    run_p.add_argument("--force", action="store_true", help="run even if the assignment is paused")

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
                    notify_agent=args.notify,
                    wake_backend=args.wake_backend,
                    wake_target=args.wake_target,
                    wake_on=args.wake_on,
                )
                log(f"tracking {assignment.id}: {assignment.text!r} every {args.interval}")
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
                    f" · ${store.total_cost(assignment.id):.3f} spent"
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
                    print(
                        f"  {stat.name}: {stat.listings} listings, {stat.priced} priced "
                        f"({stat.price_rate:.0%}), from {_money(stat.cheapest, stat.currency)}, "
                        f"median {_money(stat.median, stat.currency)}"
                    )
                return 0

            if args.command == "run":
                assignment = _require(store, args.assignment_id)
                if assignment.status != "active" and not args.force:
                    print(
                        f"track: error: {assignment.id} is {assignment.status} "
                        "(use --force to run anyway)",
                        file=sys.stderr,
                    )
                    return 1
                _findings, summary = run_assignment(
                    store, assignment, warn=log, post=not args.no_post
                )
                print(summary)
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
