"""Shared exceptions for track."""

from __future__ import annotations


class TrackError(Exception):
    """Base error for all track failures."""


class StoreError(TrackError):
    """Raised when the persistence layer cannot complete an operation."""


class ScoutError(TrackError):
    """Raised when a Sonnet scout run fails or returns unparsable output."""


class SchedulerError(TrackError):
    """Raised when a wakeup cannot be scheduled or cancelled."""


class ReportError(TrackError):
    """Raised when a summary cannot be posted."""
