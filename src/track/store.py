"""SQLite persistence for track: assignments, sources, findings, runs.

Findings are append-only: every run's rows are inserted, never rewritten or
rescored. `underpriced_score` is computed against the price history that
existed *at the time* of that finding -- a later run's prices don't reach
back and change how an earlier finding was scored.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import StoreError
from .models import Assignment, Finding, Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    max_price REAL,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT,
    job_id TEXT,
    backend TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL REFERENCES assignments(id),
    name TEXT NOT NULL,
    url TEXT,
    notes TEXT,
    times_seen INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(assignment_id, name)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL REFERENCES assignments(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    scout_count INTEGER NOT NULL DEFAULT 0,
    findings_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL REFERENCES assignments(id),
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL,
    currency TEXT,
    url TEXT,
    dedup_key TEXT NOT NULL,
    score REAL,
    is_new INTEGER NOT NULL,
    found_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_assignment ON findings(assignment_id);
CREATE INDEX IF NOT EXISTS idx_findings_dedup ON findings(assignment_id, dedup_key);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "track" / "track.db"


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.db_path)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database at {self.db_path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- assignments ---------------------------------------------------

    def add_assignment(
        self, text: str, interval_seconds: int, max_price: float | None = None
    ) -> Assignment:
        assignment_id = uuid.uuid4().hex[:8]
        self._conn.execute(
            "INSERT INTO assignments (id, text, interval_seconds, status, max_price, "
            "created_at, last_run_at, next_run_at, job_id, backend) "
            "VALUES (?, ?, ?, 'active', ?, ?, NULL, NULL, NULL, NULL)",
            (assignment_id, text, interval_seconds, max_price, _now()),
        )
        self._conn.commit()
        assignment = self.get_assignment(assignment_id)
        if assignment is None:
            raise StoreError("assignment vanished immediately after insert")
        return assignment

    def get_assignment(self, assignment_id: str) -> Assignment | None:
        row = self._conn.execute(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        return _row_to_assignment(row) if row else None

    def list_assignments(self) -> list[Assignment]:
        rows = self._conn.execute("SELECT * FROM assignments ORDER BY created_at").fetchall()
        return [_row_to_assignment(row) for row in rows]

    def set_schedule(
        self, assignment_id: str, job_id: str, backend: str, next_run_at: str
    ) -> None:
        self._conn.execute(
            "UPDATE assignments SET job_id = ?, backend = ?, next_run_at = ? WHERE id = ?",
            (job_id, backend, next_run_at, assignment_id),
        )
        self._conn.commit()

    def clear_schedule(self, assignment_id: str) -> None:
        self._conn.execute(
            "UPDATE assignments SET job_id = NULL, backend = NULL, next_run_at = NULL WHERE id = ?",
            (assignment_id,),
        )
        self._conn.commit()

    def set_status(self, assignment_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE assignments SET status = ? WHERE id = ?", (status, assignment_id)
        )
        self._conn.commit()

    def mark_ran(self, assignment_id: str) -> None:
        self._conn.execute(
            "UPDATE assignments SET last_run_at = ? WHERE id = ?", (_now(), assignment_id)
        )
        self._conn.commit()

    def remove_assignment(self, assignment_id: str) -> None:
        self._conn.execute("DELETE FROM findings WHERE assignment_id = ?", (assignment_id,))
        self._conn.execute("DELETE FROM runs WHERE assignment_id = ?", (assignment_id,))
        self._conn.execute("DELETE FROM sources WHERE assignment_id = ?", (assignment_id,))
        self._conn.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
        self._conn.commit()

    # -- sources ---------------------------------------------------------

    def upsert_source(
        self, assignment_id: str, name: str, url: str | None = None, notes: str | None = None
    ) -> None:
        now = _now()
        existing = self._conn.execute(
            "SELECT id FROM sources WHERE assignment_id = ? AND name = ?", (assignment_id, name)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE sources SET times_seen = times_seen + 1, last_seen_at = ?, "
                "url = COALESCE(?, url), notes = COALESCE(?, notes) WHERE id = ?",
                (now, url, notes, existing["id"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO sources (assignment_id, name, url, notes, times_seen, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                (assignment_id, name, url, notes, now, now),
            )
        self._conn.commit()

    def list_sources(self, assignment_id: str) -> list[Source]:
        rows = self._conn.execute(
            "SELECT * FROM sources WHERE assignment_id = ? ORDER BY times_seen DESC",
            (assignment_id,),
        ).fetchall()
        return [_row_to_source(row) for row in rows]

    # -- runs --------------------------------------------------------------

    def start_run(self, assignment_id: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO runs (assignment_id, started_at) VALUES (?, ?)",
            (assignment_id, _now()),
        )
        self._conn.commit()
        run_id = cursor.lastrowid
        if run_id is None:
            raise StoreError("run insert did not produce a rowid")
        return run_id

    def finish_run(self, run_id: int, scout_count: int, findings_count: int) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, scout_count = ?, findings_count = ? WHERE id = ?",
            (_now(), scout_count, findings_count, run_id),
        )
        self._conn.commit()

    # -- findings ------------------------------------------------------------

    def price_history(self, assignment_id: str) -> list[float]:
        rows = self._conn.execute(
            "SELECT price FROM findings WHERE assignment_id = ? AND price IS NOT NULL",
            (assignment_id,),
        ).fetchall()
        return [row["price"] for row in rows]

    def has_seen(self, assignment_id: str, dedup_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM findings WHERE assignment_id = ? AND dedup_key = ? LIMIT 1",
            (assignment_id, dedup_key),
        ).fetchone()
        return row is not None

    def add_finding(
        self,
        assignment_id: str,
        run_id: int,
        source: str,
        title: str,
        price: float | None,
        currency: str | None,
        url: str | None,
        dedup_key: str,
        score: float | None,
        is_new: bool,
    ) -> Finding:
        cursor = self._conn.execute(
            "INSERT INTO findings (assignment_id, run_id, source, title, price, currency, "
            "url, dedup_key, score, is_new, found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assignment_id,
                run_id,
                source,
                title,
                price,
                currency,
                url,
                dedup_key,
                score,
                int(is_new),
                _now(),
            ),
        )
        self._conn.commit()
        finding_id = cursor.lastrowid
        if finding_id is None:
            raise StoreError("finding insert did not produce a rowid")
        row = self._conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        return _row_to_finding(row)

    def run_findings(self, run_id: int) -> list[Finding]:
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY score DESC", (run_id,)
        ).fetchall()
        return [_row_to_finding(row) for row in rows]

    def best_findings(self, assignment_id: str, limit: int = 5) -> list[Finding]:
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE assignment_id = ? ORDER BY score DESC LIMIT ?",
            (assignment_id, limit),
        ).fetchall()
        return [_row_to_finding(row) for row in rows]


def _row_to_assignment(row: sqlite3.Row) -> Assignment:
    return Assignment(
        id=row["id"],
        text=row["text"],
        interval_seconds=row["interval_seconds"],
        status=row["status"],
        max_price=row["max_price"],
        created_at=row["created_at"],
        last_run_at=row["last_run_at"],
        next_run_at=row["next_run_at"],
        job_id=row["job_id"],
        backend=row["backend"],
    )


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        assignment_id=row["assignment_id"],
        name=row["name"],
        url=row["url"],
        notes=row["notes"],
        times_seen=row["times_seen"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
    )


def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"],
        assignment_id=row["assignment_id"],
        run_id=row["run_id"],
        source=row["source"],
        title=row["title"],
        price=row["price"],
        currency=row["currency"],
        url=row["url"],
        dedup_key=row["dedup_key"],
        score=row["score"],
        is_new=bool(row["is_new"]),
        found_at=row["found_at"],
    )
