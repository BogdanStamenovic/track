"""track: give it an assignment, it hunts who sells that cheap and where, then keeps watching."""

from __future__ import annotations

__version__ = "0.1.0"

from .cli import main
from .errors import ReportError, SchedulerError, ScoutError, StoreError, TrackError

__all__ = [
    "ReportError",
    "SchedulerError",
    "ScoutError",
    "StoreError",
    "TrackError",
    "__version__",
    "main",
]
