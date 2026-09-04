"""Data model for track."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Assignment:
    id: str
    text: str
    interval_seconds: int
    status: str  # "active" | "paused"
    max_price: float | None
    created_at: str
    last_run_at: str | None = None
    next_run_at: str | None = None
    job_id: str | None = None
    backend: str | None = None  # "wake" | "systemd-timer"
    market: str | None = None  # where the buyer is; a hard constraint on sources
    notify_agent: str | None = None  # hotline agent whose channel gets the summary
    wake_backend: str | None = None  # "shell" | "rtcwake" | "wol"
    wake_target: str | None = None  # MAC address, for the wol backend
    wake_on: str | None = None  # which machine runs the task (wake's --on)
    resume_job_id: str | None = None  # the paired rtcwake/wol task, if any
    runs_count: int = 0


@dataclass(frozen=True, slots=True)
class Source:
    id: int
    assignment_id: str
    name: str
    url: str | None
    notes: str | None
    times_seen: int
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True, slots=True)
class Finding:
    id: int
    assignment_id: str
    run_id: int
    source: str
    title: str
    price: float | None
    currency: str | None
    url: str | None
    dedup_key: str
    score: float | None
    is_new: bool
    found_at: str
    # Why the score is what it is. `score_basis` is "mispricing" when a
    # comparable was found and "cheapness" when the score fell back to a
    # percentile of everything on record, which is a much weaker claim.
    reference_price: float | None = None
    reference_n: int | None = None
    score_basis: str | None = None
    # Provenance, captured at scrape time. "How old is it" is two different
    # questions and they are stored separately: `listing_posted_at` /
    # `listing_age_days` are the *advert's* age, `product_year` is the
    # *model's*. A 2018 ThinkPad posted yesterday is a new listing of an old
    # machine, and a reader needs both to judge it.
    rationale: str | None = None  # the scout's own words on why this one
    condition: str | None = None
    listing_posted_at: str | None = None
    listing_age_days: float | None = None
    product_year: int | None = None


@dataclass(frozen=True, slots=True)
class ListingStatus:
    """What has become of one listing, as against what a run saw of it.

    Kept apart from `findings` because findings are append-only sightings and
    this is mutable state about *now*: how many times we have failed to reach
    the page, and whether it has been retired. Folding it into the sighting
    rows would mean rewriting history to record a fact about the present.
    """

    assignment_id: str
    dedup_key: str
    first_seen_at: str
    last_seen_at: str
    times_seen: int
    last_checked_at: str | None = None
    check_failures: int = 0
    # The last thing a check established, retired or not -- including "the
    # site refused the request", which is why it is kept apart from
    # `retired_note`. A field whose name says "retired" while holding the
    # outcome of a check on a live listing is a field that will be read wrong.
    last_check_note: str | None = None
    retired_at: str | None = None
    retired_reason: str | None = None  # "gone" | "superseded"
    retired_note: str | None = None  # why it was retired, set only when it was
    superseded_by: str | None = None  # dedup_key of the listing that beat it


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    assignment_id: str
    started_at: str
    finished_at: str | None
    scout_count: int
    findings_count: int
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class SourceStat:
    """What one source has actually been worth for an assignment."""

    name: str
    listings: int
    priced: int  # listings that came back with a real price
    cheapest: float | None
    median: float | None
    best_score: float | None
    currency: str | None

    @property
    def price_rate(self) -> float:
        """Share of this source's listings that yielded a usable price.

        A source that blocks reads sits near 0 here while still contributing
        listings, which is the honest way to show "we can see it exists but
        not what it costs".
        """
        return self.priced / self.listings if self.listings else 0.0
