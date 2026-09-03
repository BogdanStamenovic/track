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
