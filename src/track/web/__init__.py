"""The browsable half of track: a local, read-only site over the findings it has collected.

Imported only when `track web` actually runs, so a box that only runs the
scheduled research never loads it. Nothing here writes to the database.
"""

from __future__ import annotations

from ._cli import main
from .data import Assignment, Listing, Schema, WebError

__all__ = [
    "Assignment",
    "Listing",
    "Schema",
    "WebError",
    "main",
]
