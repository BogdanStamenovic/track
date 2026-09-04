"""Database seeds for the `track web` tests.

A plain module rather than a second conftest: `tests/conftest.py` belongs to the
rest of track, and one owner per file is what keeps this repo out of merge
conflicts.

It exports seed *functions* rather than fixtures on purpose. Importing a fixture
into a module that also names it as a test parameter reads to every linter as a
redefinition, and the fix for that is 37 suppressions. Four small fixtures that
call these instead cost less and hide nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_SCHEMA = """
CREATE TABLE assignments (
    id TEXT PRIMARY KEY, text TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', max_price REAL, created_at TEXT NOT NULL,
    last_run_at TEXT, next_run_at TEXT, job_id TEXT, backend TEXT, market TEXT,
    notify_agent TEXT, wake_backend TEXT, wake_target TEXT, wake_on TEXT,
    resume_job_id TEXT, runs_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, scout_count INTEGER NOT NULL DEFAULT 0,
    findings_count INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0);
CREATE TABLE findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL,
    run_id INTEGER NOT NULL, source TEXT NOT NULL, title TEXT NOT NULL, price REAL,
    currency TEXT, url TEXT, dedup_key TEXT NOT NULL, score REAL,
    is_new INTEGER NOT NULL, found_at TEXT NOT NULL);
"""

#: What track-core is adding. Named from COLUMN_CANDIDATES so the "future" test
#: exercises the resolver rather than hardcoding a contract that is not agreed.
#: The names track-core actually shipped on 2026-09-04, plus the retirement
#: columns the reaper still owes. Kept spelled exactly as the live table has
#: them so the fixture tests the real contract, not a convenient one.
FUTURE_COLUMNS = [
    "ALTER TABLE findings ADD COLUMN rationale TEXT",
    "ALTER TABLE findings ADD COLUMN listing_posted_at TEXT",
    "ALTER TABLE findings ADD COLUMN listing_age_days INTEGER",
    "ALTER TABLE findings ADD COLUMN product_year INTEGER",
    "ALTER TABLE findings ADD COLUMN condition TEXT",
]

#: Retirement lives in its own table, not on the append-only sighting rows.
#: Spelled exactly as track creates it so these tests exercise the real shape.
STATUS_TABLE = """
CREATE TABLE listing_status (
    assignment_id   TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    check_failures  INTEGER NOT NULL DEFAULT 0,
    retired_at      TEXT,
    retired_reason  TEXT,
    retired_note    TEXT,
    superseded_by   TEXT,
    last_check_note TEXT,
    PRIMARY KEY (assignment_id, dedup_key)
);
"""


def _rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO findings (assignment_id, run_id, source, title, price, currency,"
        " url, dedup_key, score, is_new, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _seed(path: Path, *, future: bool) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(BASE_SCHEMA)
    if future:
        for stmt in FUTURE_COLUMNS:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO assignments (id, text, interval_seconds, status, created_at,"
        " last_run_at, market, runs_count) VALUES (?,?,?,?,?,?,?,?)",
        ("aa11", "a test hunt: with a long brief that goes on and on and on",
         3600, "active", "2026-09-01T00:00:00+00:00", "2026-09-04T00:00:00+00:00",
         "Serbia", 2),
    )
    conn.execute(
        "INSERT INTO runs (id, assignment_id, started_at) VALUES (1, 'aa11', ?)",
        ("2026-09-01T00:00:00+00:00",),
    )
    conn.execute(
        "INSERT INTO runs (id, assignment_id, started_at) VALUES (2, 'aa11', ?)",
        ("2026-09-04T00:00:00+00:00",),
    )
    _rows(conn, [
        # same listing twice; the second sighting lost its price and its score
        ("aa11", 1, "ShopA", "Widget 3000", 500.0, "EUR", "https://a/x", "k1", 0.9, 1,
         "2026-09-01T10:00:00+00:00"),
        ("aa11", 2, "ShopA", "Widget 3000", None, None, "https://a/x", "k1", None, 0,
         "2026-09-04T10:00:00+00:00"),
        # never had a price or a score or a link
        ("aa11", 2, "ShopB", "Mystery Box", None, None, None, "k2", None, 1,
         "2026-09-04T10:05:00+00:00"),
        # cheap and scored
        ("aa11", 2, "ShopC", "Widget 1000", 120.0, "EUR", "https://c/y", "k3", 0.4, 1,
         "2026-09-04T10:06:00+00:00"),
    ])
    conn.commit()
    conn.close()
    return path


def seed_legacy(tmp_path: Path) -> Path:
    """The bare schema: no rationale, no age, no listing_status."""
    return _seed(tmp_path / "legacy.db", future=False)


def seed_future(tmp_path: Path) -> Path:
    """The schema as track actually has it: new columns plus listing_status."""
    path = _seed(tmp_path / "future.db", future=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE findings SET rationale = ?, listing_posted_at = ?, product_year = 2025,"
        " condition = 'refurbished' WHERE dedup_key = 'k1'",
        ("Cheapest 32GB machine seen in this market by 18%.", "2026-08-20T00:00:00+00:00"),
    )
    conn.execute(
        "UPDATE findings SET rationale = ? WHERE dedup_key = 'k3'",
        ("Was the price leader until it sold.",),
    )
    conn.executescript(STATUS_TABLE)
    conn.executemany(
        "INSERT INTO listing_status (assignment_id, dedup_key, first_seen_at, last_seen_at,"
        " times_seen, last_checked_at, check_failures, retired_at, retired_reason,"
        " retired_note, superseded_by, last_check_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # live and confirmed
            ("aa11", "k1", "2026-09-01T10:00:00+00:00", "2026-09-04T10:00:00+00:00", 2,
             "2026-09-04T11:00:00+00:00", 0, None, None, None, None, "still listed"),
            # live, but the last check could not reach it -- NOT dead
            ("aa11", "k2", "2026-09-04T10:05:00+00:00", "2026-09-04T10:05:00+00:00", 1,
             "2026-09-04T11:00:00+00:00", 2, None, None, None, None,
             "blocked: 403 from the source"),
            # genuinely retired
            ("aa11", "k3", "2026-09-04T10:06:00+00:00", "2026-09-04T10:06:00+00:00", 1,
             "2026-09-04T12:00:00+00:00", 0, "2026-09-04T12:00:00+00:00", "gone",
             "the seller marked it sold", None, "page returns 404"),
        ],
    )
    conn.commit()
    conn.close()
    return path
