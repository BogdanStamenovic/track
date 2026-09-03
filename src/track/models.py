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
    last_run_at: str | None
    next_run_at: str | None
    job_id: str | None
    backend: str | None  # "wake" | "systemd-timer" | None


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
