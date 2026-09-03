"""Builds a run summary and posts it to Discord via hotline-say.

Delivery has one non-obvious constraint. `hotline-say` resolves which
channel to post in from the *calling agent's* Claude session id, and a
scheduled `track run` has no session -- it is a bare process started by
`wake` or systemd. Left alone it exits 1 with "no channel for this session",
which is precisely the run you most wanted to hear about. So a target is
resolved explicitly, in this order: the assignment's own `notify_agent`,
then TRACK_HOTLINE_CHANNEL / TRACK_HOTLINE_AGENT from the environment, and
only then the session default (which is what you want when a human runs
`track run` by hand from inside a session).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .errors import ReportError
from .models import Assignment, Finding, SourceStat

HOTLINE_SAY_BIN = "hotline-say"
_FALLBACK_PATH = Path.home() / ".claude" / "bin" / HOTLINE_SAY_BIN

TOP_N = 5
TOP_SOURCES = 3


def _money(price: float | None, currency: str | None) -> str:
    if price is None:
        return "price unknown"
    return f"{price:,.2f} {currency}" if currency else f"{price:,.2f}"


def build_summary(
    assignment: Assignment,
    findings: list[Finding],
    sources_checked: int,
    *,
    stats: list[SourceStat] | None = None,
    cost_usd: float = 0.0,
    schedule_error: str | None = None,
) -> str:
    """A tight Discord-shaped summary of one run."""
    over_ceiling = 0
    new_findings = []
    for f in findings:
        if not f.is_new:
            continue
        if assignment.max_price is not None and f.price is not None and f.price > assignment.max_price:
            over_ceiling += 1
            continue
        new_findings.append(f)

    header = (
        f'**track** — "{assignment.text}"\n'
        f"{sources_checked} sources checked · {len(findings)} listings seen · "
        f"{len(new_findings)} new"
    )
    if over_ceiling:
        header += f" · {over_ceiling} over the {_money(assignment.max_price, None)} ceiling"
    lines = [header]

    if not new_findings:
        lines.append("\nNothing new this run.")
    else:
        ranked = sorted(new_findings, key=lambda f: (f.score if f.score is not None else -1.0), reverse=True)
        lines.append("")
        for f in ranked[:TOP_N]:
            score_tag = f" · score {f.score:.2f}" if f.score is not None else ""
            lines.append(f"• **{f.title}** — {_money(f.price, f.currency)} @ {f.source}{score_tag}")
            if f.url:
                lines.append(f"  <{f.url}>")
        if len(ranked) > TOP_N:
            lines.append(f"…and {len(ranked) - TOP_N} more new listings.")

    priced_stats = [s for s in (stats or []) if s.cheapest is not None]
    if priced_stats:
        lines.append("\nCheapest sources so far:")
        for s in priced_stats[:TOP_SOURCES]:
            lines.append(
                f"  {s.name}: from {_money(s.cheapest, s.currency)} "
                f"(median {_money(s.median, s.currency)}, {s.listings} listings)"
            )
    unreadable = [s for s in (stats or []) if s.listings and s.priced == 0]
    if unreadable:
        names = ", ".join(s.name for s in unreadable[:TOP_SOURCES])
        lines.append(f"  no price readable from: {names}")

    if cost_usd:
        lines.append(f"\n_scouts: ${cost_usd:.3f}_")
    if schedule_error:
        # Last line and unmissable: this run is the last one unless someone
        # intervenes, which is more important than anything it found.
        lines.append(
            f"\n**:warning: {schedule_error}** — this assignment will not run "
            f"again until it is rescheduled (`track resume {assignment.id}`)."
        )
    return "\n".join(lines)


def _resolve_hotline_say() -> str:
    found = shutil.which(HOTLINE_SAY_BIN)
    if found:
        return found
    if _FALLBACK_PATH.exists():
        return str(_FALLBACK_PATH)
    raise ReportError(f"{HOTLINE_SAY_BIN} not found on PATH or at {_FALLBACK_PATH}")


def _delivery_args(agent: str | None) -> list[str]:
    if agent:
        return ["--agent", agent]
    channel = os.environ.get("TRACK_HOTLINE_CHANNEL")
    if channel:
        return ["--channel", channel]
    env_agent = os.environ.get("TRACK_HOTLINE_AGENT")
    if env_agent:
        return ["--agent", env_agent]
    return []  # in-session default: hotline-say resolves it from the session id


def post_summary(summary: str, *, agent: str | None = None) -> None:
    cmd = [_resolve_hotline_say(), *_delivery_args(agent), summary]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ReportError("hotline-say timed out after 60s") from exc
    if result.returncode != 0:
        raise ReportError(f"hotline-say exited {result.returncode}: {result.stderr.strip()[:500]}")
