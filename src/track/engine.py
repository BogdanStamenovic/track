"""Orchestrates one research cycle for an assignment.

Ties together scouting, scoring, storage and reporting, and -- when the
assignment is scheduled through `wake` -- re-arms its own next wakeup before
returning, since wake tasks are one-shot (see scheduler.py). A
`systemd-timer`-backed assignment already recurs on its own.

Two ordering rules that the correctness of the scores depends on:

* The price history is snapshotted once, before any finding is scored, and
  not extended during the run. Scoring against a history that grows as the
  run proceeds would make a finding's score depend on which scout happened
  to return first, so two identical runs could disagree.
* Findings are only ever appended. A run scores its own findings against the
  history that existed when it started and never touches an earlier row, so
  history means "what this looked like at the time", permanently.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from . import scheduler, scouts
from .errors import TrackError
from .models import Assignment, Finding, SourceStat
from .report import build_summary, post_summary
from .scoring import dedup_key, source_stats, underpriced_score
from .store import Store

DEFAULT_SOURCE_LIMIT = 5


def run_command(store: Store, assignment_id: str) -> list[str]:
    """How a scheduler should spell "run this assignment".

    A fired task inherits nothing from the process that scheduled it -- not
    PATH, not the working directory, not which database was in use. So both
    are resolved here and written into the task itself: a bare `track` is not
    on a systemd or wake unit's PATH, and an assignment created in a
    non-default database would otherwise schedule a run that looks for itself
    in the default one and reports no such assignment.

    It lives in one function because the re-arm path rewrites the task string
    every run; the two spellings drifting apart means the first scheduled run
    works and every one after it does not.
    """
    track_bin = shutil.which("track") or str(Path(sys.argv[0]).resolve())
    return [track_bin, "--db", str(Path(store.db_path).resolve()), "run", assignment_id]

# Re-run source discovery every N runs even when sources are already known.
# Without this the source list is frozen at whatever the very first scout
# thought, and the whole "who sells this cheap" question stops being asked
# the moment it has been answered once.
REDISCOVER_EVERY = 10


def _should_rediscover(assignment: Assignment, known: int) -> bool:
    if known == 0:
        return True
    return assignment.runs_count > 0 and assignment.runs_count % REDISCOVER_EVERY == 0


def ensure_sources(
    store: Store, assignment: Assignment, *, warn: Callable[[str], None] = lambda _m: None
) -> tuple[list[str], float]:
    """Known source names for an assignment, discovering more when it's due.

    Returns the names and what discovery cost (0.0 when it didn't run).
    """
    existing = [s.name for s in store.list_sources(assignment.id)]
    if not _should_rediscover(assignment, len(existing)):
        return existing, 0.0
    try:
        discovered, cost = scouts.discover_sources(assignment.text)
    except TrackError as exc:
        warn(f"source discovery failed: {exc}")
        return existing, 0.0
    names = list(existing)
    for item in discovered:
        name = item.get("source")
        if not name:
            continue
        name = str(name).strip()
        if not name:
            continue
        store.upsert_source(assignment.id, name, item.get("url"), item.get("notes"))
        if name not in names:
            names.append(name)
    return names, cost


def _rearm_wake(
    store: Store, assignment: Assignment, *, warn: Callable[[str], None]
) -> str | None:
    """Arm the next wakeup. Returns a message if it could not be armed.

    The caller reports that message rather than only logging it, because a
    failure here is silent and terminal: the assignment simply never runs
    again, and the stderr of a run nobody launched is read by nobody. It very
    nearly happened for real -- `wake add --id X` on an existing id raised an
    IntegrityError until wake e04e149, which would have killed every
    assignment on its second run with no signal but a line on a dead stream.
    """
    if assignment.backend != "wake" or assignment.status != "active":
        return None
    try:
        result = scheduler.schedule(
            assignment.id,
            assignment.interval_seconds,
            run_command(store, assignment.id),
            wake_backend=assignment.wake_backend or "shell",
            target=assignment.wake_target,
            run_on=assignment.wake_on,
            wake_available=True,
        )
    except TrackError as exc:
        message = f"could not schedule the next check: {exc}"
        warn(message)
        return message
    store.set_schedule(
        assignment.id, result.job_id, result.backend, result.next_run_at, result.resume_job_id
    )
    return None


def run_assignment(
    store: Store,
    assignment: Assignment,
    *,
    warn: Callable[[str], None] = lambda _m: None,
    post: bool = True,
) -> tuple[list[Finding], str]:
    source_names, discovery_cost = ensure_sources(store, assignment, warn=warn)
    run_id = store.start_run(assignment.id)

    scouted = scouts.run_scouts(
        assignment.text, source_names[:DEFAULT_SOURCE_LIMIT], warn=warn
    )
    if scouted.blocked:
        warn(
            f"{scouted.blocked} listing(s) came back without a price -- "
            "the site would not serve one"
        )

    history = store.price_history(assignment.id)  # frozen for the whole run, on purpose
    stored: list[Finding] = []
    for raw in scouted.findings:
        key = dedup_key(raw.source, raw.title, raw.url)
        is_new = not store.has_seen(assignment.id, key)
        score = underpriced_score(raw.price, history) if raw.price is not None else None
        stored.append(
            store.add_finding(
                assignment.id,
                run_id,
                raw.source,
                raw.title,
                raw.price,
                raw.currency,
                raw.url,
                key,
                score,
                is_new,
            )
        )
        store.upsert_source(assignment.id, raw.source, raw.url)

    cost = discovery_cost + scouted.cost_usd
    store.finish_run(run_id, len(source_names), len(stored), cost)
    store.mark_ran(assignment.id)

    # Re-arm before the summary is built, not after posting it: a run that
    # cannot schedule its successor is the last one that will ever happen,
    # and that belongs in the message rather than in a log nobody opens.
    # Re-read first -- mark_ran bumped runs_count and the row we were handed
    # is frozen.
    refreshed = store.get_assignment(assignment.id) or assignment
    schedule_error = _rearm_wake(store, refreshed, warn=warn)

    stats: list[SourceStat] = source_stats(store.latest_findings(assignment.id))
    summary = build_summary(
        assignment,
        stored,
        len(source_names),
        stats=stats,
        cost_usd=cost,
        schedule_error=schedule_error,
    )
    if post:
        try:
            post_summary(summary, agent=assignment.notify_agent)
        except TrackError as exc:
            warn(f"could not post summary: {exc}")

    return stored, summary
