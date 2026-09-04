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

# Below this many priced finds, thirds are noise -- "budget / mid / stretch"
# drawn from four listings says more about the sample than the market.
MIN_FOR_TIERS = 6


def _money(price: float | None, currency: str | None) -> str:
    if price is None:
        return "price unknown"
    return f"{price:,.2f} {currency}" if currency else f"{price:,.2f}"


def _tier(findings: list[Finding]) -> list[tuple[str, Finding]]:
    """Split priced finds into budget / mid / stretch and pick one of each.

    Tiers are cut by price within a single currency -- see the caller, which
    only ever hands over one currency's worth. Within a tier the pick is the
    highest-scoring listing, not the cheapest, because the cheapest thing in
    the budget tier is usually the one with the worst specifications and the
    question being answered is "what is the best buy at roughly this price".
    """
    ranked = sorted(findings, key=lambda f: f.price or 0.0)
    third = max(1, len(ranked) // 3)
    bands = [
        ("Budget", ranked[:third]),
        ("Mid", ranked[third : third * 2]),
        ("Stretch", ranked[third * 2 :]),
    ]
    out: list[tuple[str, Finding]] = []
    for label, band in bands:
        if band:
            out.append((label, max(band, key=lambda f: (f.score or 0.0, -(f.price or 0.0)))))
    return out


def build_summary(
    assignment: Assignment,
    findings: list[Finding],
    sources_checked: int,
    *,
    stats: list[SourceStat] | None = None,
    cost_usd: float = 0.0,
    schedule_error: str | None = None,
    scout_failures: int = 0,
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
        # An empty run has two very different causes and the reader cannot
        # tell them apart from "nothing new": the market was quiet, or the
        # research did not happen. Say which.
        if scout_failures:
            lines.append(
                f"\nNothing new this run — but {scout_failures} scout(s) failed, "
                "so this is not evidence the market is quiet."
            )
        else:
            lines.append("\nNothing new this run.")
    else:
        lines.extend(_body(new_findings))

    priced_stats = [s for s in (stats or []) if s.cheapest is not None]
    if priced_stats:
        # "Cheapest" only ranks within a currency, so say so when more than one
        # is present rather than implying 3000 RSD undercuts 30 EUR.
        currencies = {s.currency for s in priced_stats}
        heading = (
            "\nCheapest sources so far (per currency):"
            if len(currencies) > 1
            else "\nCheapest sources so far:"
        )
        lines.append(heading)
        for s in priced_stats[:TOP_SOURCES]:
            lines.append(
                f"  {s.name}: from {_money(s.cheapest, s.currency)} "
                f"(median {_money(s.median, s.currency)}, {s.listings} listings)"
            )
    # Only sources with *no* readable price anywhere. Stats are keyed by
    # (source, currency), so a site that quotes in two currencies and also has
    # some unpriced listings produces an unpriced group as well -- naming it
    # unreadable next to its own prices reads as a contradiction.
    priced_names = {s.name for s in (stats or []) if s.priced}
    unreadable = [
        s for s in (stats or []) if s.listings and not s.priced and s.name not in priced_names
    ]
    if unreadable:
        names = ", ".join(dict.fromkeys(s.name for s in unreadable))
        lines.append(f"  no price readable from: {names}")

    if scout_failures and new_findings:
        lines.append(f"\n_{scout_failures} scout(s) failed; this run saw less than usual._")
    if cost_usd:
        # Labelled, not just printed. The number is `total_cost_usd` out of the
        # `claude -p` JSON envelope, which the CLI emits whatever it is
        # authenticated with -- so on a subscription it is an API-list-price
        # equivalent of the work done and bills nothing, and only on an API key
        # is it money. Unqualified it reads as a charge, and the first person
        # to see it in Discord asked whether it was hitting his card.
        lines.append(
            f"\n_scouts: ~${cost_usd:.2f} of model usage at list price "
            "(no charge on a Claude subscription)_"
        )
    if schedule_error:
        # Last line and unmissable: this run is the last one unless someone
        # intervenes, which is more important than anything it found.
        lines.append(
            f"\n**:warning: {schedule_error}** — this assignment will not run "
            f"again until it is rescheduled (`track resume {assignment.id}`)."
        )
    return "\n".join(lines)


def _body(new_findings: list[Finding]) -> list[str]:
    """The findings section: tiered when there is enough to tier, ranked when not."""
    priced_by_currency: dict[str | None, list[Finding]] = {}
    for f in new_findings:
        if f.price is not None:
            priced_by_currency.setdefault(f.currency, []).append(f)

    # Tier within the currency that has the most listings. Cutting thirds
    # across currencies would sort every dinar price into "stretch" and every
    # euro one into "budget" purely by the size of the number.
    main_group: list[Finding] = []
    if priced_by_currency:
        main_group = max(priced_by_currency.values(), key=len)

    if len(main_group) < MIN_FOR_TIERS:
        return _ranked_lines(new_findings, TOP_N)

    lines = [""]
    if len(priced_by_currency) > 1:
        # Say which currency the tiers came from. track does not convert, so
        # the other currency's listings are genuinely unranked against these,
        # and a reader who does not know that will read their absence from the
        # tiers as a judgement on them.
        currency = main_group[0].currency or "unpriced"
        lines.append(f"_Tiers below are {currency} listings; other currencies follow._")
    for label, f in _tier(main_group):
        lines.append(f"**{label}** · {_money(f.price, f.currency)} — {f.title} @ {f.source}")
        lines.extend(_detail(f))

    others = [f for f in new_findings if f not in main_group]
    if others:
        other_currencies = sorted({f.currency for f in others if f.currency})
        tag = f" in {', '.join(other_currencies)}" if other_currencies else ""
        lines.append(f"\nAlso new{tag} ({len(others)}), best first:")
        lines.extend(_ranked_lines(others, 3)[1:])
    return lines


def _age(f: Finding) -> str:
    """The listing's age and the product's, which are different questions.

    A 2018 ThinkPad posted yesterday is a new advert for an old machine, and
    a 2024 machine that has sat unsold for three months is the opposite. Both
    matter to someone deciding whether to open the link, and neither is
    recoverable from the other.
    """
    parts = []
    if f.listing_age_days is not None:
        days = int(f.listing_age_days)
        parts.append("listed today" if days < 1 else f"listed {days}d ago")
    elif f.listing_posted_at:
        parts.append(f"listed {f.listing_posted_at}")
    if f.product_year:
        parts.append(f"{f.product_year} model")
    if f.condition:
        parts.append(f.condition)
    return " · ".join(parts)


def _detail(f: Finding) -> list[str]:
    """The lines under a listing: why, how old, and the link."""
    lines = []
    if f.rationale:
        lines.append(f"  _{f.rationale}_")
    verdict = _verdict(f)
    age = _age(f)
    tail = " · ".join(x for x in (verdict, age) if x)
    if tail:
        lines.append(f"  {tail}")
    if f.url:
        lines.append(f"  <{f.url}>")
    return lines


def _verdict(f: Finding) -> str:
    """What the score is claiming, and on what evidence.

    A bare "score 0.62" is unreadable without knowing what it was measured
    against, and the two bases mean very different things: one says "this is
    23% under what four other listings of the same card are asking", the
    other only says "this is a small number compared to everything we have
    seen". Both belong in the report; conflating them does not.
    """
    if f.score is None:
        return ""
    if f.score_basis == "mispricing" and f.reference_price:
        peers = f.reference_n or 0
        gap = (f.reference_price - (f.price or 0.0)) / f.reference_price
        direction = "under" if gap >= 0 else "over"
        return (
            f"score {f.score:.2f} — **{abs(gap):.0%} {direction}** the "
            f"{_money(f.reference_price, f.currency)} that {peers} comparable "
            f"listing{'s' if peers != 1 else ''} ask"
        )
    return f"score {f.score:.2f} — _no comparable found; ranked on price alone_"


def _ranked_lines(findings: list[Finding], limit: int) -> list[str]:
    ranked = sorted(findings, key=lambda f: (f.score if f.score is not None else -1.0), reverse=True)
    lines = [""]
    for f in ranked[:limit]:
        lines.append(f"• **{f.title}** — {_money(f.price, f.currency)} @ {f.source}")
        lines.extend(_detail(f))
    if len(ranked) > limit:
        lines.append(f"…and {len(ranked) - limit} more.")
    return lines


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
