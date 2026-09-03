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

from collections.abc import Callable

from . import scheduler, scouts
from .errors import TrackError
from .models import Assignment, Finding, SourceStat
from .report import build_summary, post_summary
from .scoring import dedup_key, source_stats, underpriced_score
from .store import Store

DEFAULT_SOURCE_LIMIT = 5

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


def _rearm_wake(store: Store, assignment: Assignment, *, warn: Callable[[str], None]) -> None:
    """Arm the next wakeup. Idempotent -- see scheduler.task_id_for."""
    if assignment.backend != "wake" or assignment.status != "active":
        return
    try:
        result = scheduler.schedule(
            assignment.id,
            assignment.interval_seconds,
            ["track", "run", assignment.id],
            wake_backend=assignment.wake_backend or "shell",
            target=assignment.wake_target,
            run_on=assignment.wake_on,
            wake_available=True,
        )
    except TrackError as exc:
        warn(f"could not re-arm next wakeup: {exc}")
        return
    store.set_schedule(
        assignment.id, result.job_id, result.backend, result.next_run_at, result.resume_job_id
    )


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

    stats: list[SourceStat] = source_stats(store.latest_findings(assignment.id))
    summary = build_summary(
        assignment, stored, len(source_names), stats=stats, cost_usd=cost
    )
    if post:
        try:
            post_summary(summary, agent=assignment.notify_agent)
        except TrackError as exc:
            warn(f"could not post summary: {exc}")

    # Re-read: mark_ran bumped runs_count, and the row we were handed is
    # frozen. Re-arming off the stale copy would be harmless today but is
    # exactly the kind of thing that rots.
    refreshed = store.get_assignment(assignment.id) or assignment
    _rearm_wake(store, refreshed, warn=warn)

    return stored, summary
