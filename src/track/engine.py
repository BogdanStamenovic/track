"""Orchestrates one research cycle for an assignment.

Ties together scouting, scoring, storage and reporting, and -- when the
assignment is scheduled via the `wake` backend -- re-arms its own next
wakeup before returning, since `wake` tasks are one-shot (see
scheduler.py). A `systemd-timer`-backed assignment already recurs on its
own and needs no re-arming.
"""

from __future__ import annotations

from collections.abc import Callable

from . import scheduler, scouts
from .errors import TrackError
from .models import Assignment, Finding
from .report import build_summary, post_summary
from .scoring import dedup_key, underpriced_score
from .store import Store

DEFAULT_SOURCE_LIMIT = 5


def ensure_sources(
    store: Store, assignment: Assignment, *, warn: Callable[[str], None] = lambda _m: None
) -> list[str]:
    """Return known source names, discovering them via a scout if there are none yet."""
    existing = store.list_sources(assignment.id)
    if existing:
        return [s.name for s in existing]
    try:
        discovered = scouts.discover_sources(assignment.text)
    except TrackError as exc:
        warn(f"source discovery failed: {exc}")
        return []
    names = []
    for item in discovered:
        name = item.get("source")
        if not name:
            continue
        store.upsert_source(assignment.id, str(name), item.get("url"), item.get("notes"))
        names.append(str(name))
    return names


def _rearm_wake(store: Store, assignment: Assignment, *, warn: Callable[[str], None]) -> None:
    if assignment.backend != "wake" or assignment.status != "active":
        return
    track_bin_cmd = ["track", "run", assignment.id]
    try:
        result = scheduler.schedule(
            assignment.id, assignment.interval_seconds, track_bin_cmd, wake_available=True
        )
    except TrackError as exc:
        warn(f"could not re-arm next wakeup: {exc}")
        return
    store.set_schedule(assignment.id, result.job_id, result.backend, result.next_run_at)


def run_assignment(
    store: Store,
    assignment: Assignment,
    *,
    warn: Callable[[str], None] = lambda _m: None,
    post: bool = True,
) -> tuple[list[Finding], str]:
    source_names = ensure_sources(store, assignment, warn=warn)
    run_id = store.start_run(assignment.id)

    raw_findings = scouts.run_scouts(
        assignment.text, source_names[:DEFAULT_SOURCE_LIMIT], warn=warn
    )

    history = store.price_history(assignment.id)
    stored: list[Finding] = []
    for raw in raw_findings:
        key = dedup_key(raw.source, raw.title, raw.url)
        is_new = not store.has_seen(assignment.id, key)
        score = underpriced_score(raw.price, history) if raw.price is not None else None
        finding = store.add_finding(
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
        stored.append(finding)
        if raw.price is not None:
            history.append(raw.price)
        store.upsert_source(assignment.id, raw.source, raw.url)

    store.finish_run(run_id, len(source_names), len(stored))
    store.mark_ran(assignment.id)

    summary = build_summary(assignment, stored, len(source_names))
    if post:
        try:
            post_summary(summary)
        except TrackError as exc:
            warn(f"could not post summary: {exc}")

    _rearm_wake(store, assignment, warn=warn)

    return stored, summary
