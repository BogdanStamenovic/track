"""SQLite persistence for track: assignments, sources, findings, runs.

Findings are append-only. Every run inserts its rows and nothing ever
rewrites or rescores an earlier one -- a finding's score is the score it
earned against the price history that existed when it was found, and a
later run's cheaper listing does not reach back and demote it.

Two consequences that shape the queries below. First, the same listing
legitimately appears many times (that is how a price drop is visible), so
anything reasoning about "the current market" reads `latest_findings`, which
collapses each listing to its most recent row. Second, statistics about who
sells cheap are *derived* from findings rather than kept as counters on
`sources`, because a counter can drift out of step with the rows it claims
to summarise and a query cannot.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from .errors import StoreError
from .models import Assignment, Finding, Run, Source
from .scoring import dedup_key as _dedup_key
from .scoring import index_url_bases as _index_url_bases

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

CREATE TABLE IF NOT EXISTS finding_comparables (
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    peer_dedup_key TEXT NOT NULL,
    similarity REAL NOT NULL,
    PRIMARY KEY (finding_id, peer_dedup_key)
);

CREATE TABLE IF NOT EXISTS index_urls (
    assignment_id TEXT NOT NULL,
    url_basis TEXT NOT NULL,
    noticed_at TEXT NOT NULL,
    PRIMARY KEY (assignment_id, url_basis)
);

CREATE TABLE IF NOT EXISTS schema_meta (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_assignment ON findings(assignment_id);
CREATE INDEX IF NOT EXISTS idx_findings_dedup ON findings(assignment_id, dedup_key);
CREATE INDEX IF NOT EXISTS idx_runs_assignment ON runs(assignment_id);
"""

# Columns added after the first schema shipped. Applied with ADD COLUMN at
# open time rather than a migration table: every one is nullable or has a
# default, so replaying them on an up-to-date database is a no-op.
_ADDED_COLUMNS = [
    ("assignments", "market", "TEXT"),
    ("assignments", "notify_agent", "TEXT"),
    ("assignments", "wake_backend", "TEXT"),
    ("assignments", "wake_target", "TEXT"),
    ("assignments", "wake_on", "TEXT"),
    ("assignments", "resume_job_id", "TEXT"),
    ("assignments", "runs_count", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "cost_usd", "REAL NOT NULL DEFAULT 0"),
    ("findings", "reference_price", "REAL"),
    ("findings", "reference_n", "INTEGER"),
    ("findings", "score_basis", "TEXT"),
]


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
        self._migrate()
        self._repair_dedup_keys()
        self._conn.commit()

    def _migrate(self) -> None:
        for table, column, decl in _ADDED_COLUMNS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _applied(self, name: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM schema_meta WHERE name = ?", (name,)).fetchone()
        return row is not None

    def _mark_applied(self, name: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (name, applied_at) VALUES (?, ?)", (name, _now())
        )

    def _repair_dedup_keys(self) -> None:
        """One-off repair of keys computed before index URLs were recognised.

        Runs once, over whatever history the database already holds: work out
        which URL bases were serving several listings at a time, record them,
        and recompute every finding's key. Nothing is deleted -- each row
        keeps its price, its timestamp and its run -- but rows that had been
        merged onto one key become separately reachable again, which for the
        database this shipped against means 46 listings that
        `latest_findings` could not see.
        """
        name = "repair_dedup_keys_v1"
        if self._applied(name):
            return
        rows = self._conn.execute(
            "SELECT id, assignment_id, run_id, source, title, url FROM findings"
        ).fetchall()
        if rows:
            per_run: dict[tuple[str, int], list[tuple[str | None, str]]] = {}
            for row in rows:
                per_run.setdefault((row["assignment_id"], row["run_id"]), []).append(
                    (row["url"], row["title"])
                )
            discovered: dict[str, set[str]] = {}
            for (assignment_id, _run), listings in per_run.items():
                discovered.setdefault(assignment_id, set()).update(_index_url_bases(listings))
            for assignment_id, bases in discovered.items():
                self._register_index_urls(assignment_id, bases)
            for row in rows:
                bases = discovered.get(row["assignment_id"], set())
                key = _dedup_key(row["source"], row["title"], row["url"], bases)
                self._conn.execute(
                    "UPDATE findings SET dedup_key = ? WHERE id = ?", (key, row["id"])
                )
        self._mark_applied(name)

    # -- index urls ------------------------------------------------------

    def _register_index_urls(self, assignment_id: str, bases: set[str]) -> None:
        now = _now()
        self._conn.executemany(
            "INSERT OR IGNORE INTO index_urls (assignment_id, url_basis, noticed_at) "
            "VALUES (?, ?, ?)",
            [(assignment_id, basis, now) for basis in sorted(bases)],
        )

    def register_index_urls(self, assignment_id: str, bases: set[str]) -> None:
        """Remember URLs seen serving several listings at once.

        Registration is permanent on purpose. A base that flipped back to
        "identifying" because one run happened to return a single result from
        it would key that run's listing differently from every other run's,
        splitting one listing's history in two.
        """
        if not bases:
            return
        self._register_index_urls(assignment_id, bases)
        self._conn.commit()

    def index_url_bases(self, assignment_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT url_basis FROM index_urls WHERE assignment_id = ?", (assignment_id,)
        ).fetchall()
        return {row["url_basis"] for row in rows}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- assignments ---------------------------------------------------

    def add_assignment(
        self,
        text: str,
        interval_seconds: int,
        max_price: float | None = None,
        *,
        market: str | None = None,
        notify_agent: str | None = None,
        wake_backend: str | None = None,
        wake_target: str | None = None,
        wake_on: str | None = None,
    ) -> Assignment:
        assignment_id = uuid.uuid4().hex[:8]
        self._conn.execute(
            "INSERT INTO assignments (id, text, interval_seconds, status, max_price, "
            "created_at, market, notify_agent, wake_backend, wake_target, wake_on) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (
                assignment_id,
                text,
                interval_seconds,
                max_price,
                _now(),
                market,
                notify_agent,
                wake_backend,
                wake_target,
                wake_on,
            ),
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
        self,
        assignment_id: str,
        job_id: str,
        backend: str,
        next_run_at: str,
        resume_job_id: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE assignments SET job_id = ?, backend = ?, next_run_at = ?, "
            "resume_job_id = ? WHERE id = ?",
            (job_id, backend, next_run_at, resume_job_id, assignment_id),
        )
        self._conn.commit()

    def clear_schedule(self, assignment_id: str) -> None:
        self._conn.execute(
            "UPDATE assignments SET job_id = NULL, backend = NULL, next_run_at = NULL, "
            "resume_job_id = NULL WHERE id = ?",
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
            "UPDATE assignments SET last_run_at = ?, runs_count = runs_count + 1 WHERE id = ?",
            (_now(), assignment_id),
        )
        self._conn.commit()

    def remove_assignment(self, assignment_id: str) -> None:
        self._conn.execute(
            "DELETE FROM finding_comparables WHERE finding_id IN "
            "(SELECT id FROM findings WHERE assignment_id = ?)",
            (assignment_id,),
        )
        self._conn.execute("DELETE FROM findings WHERE assignment_id = ?", (assignment_id,))
        self._conn.execute("DELETE FROM runs WHERE assignment_id = ?", (assignment_id,))
        self._conn.execute("DELETE FROM sources WHERE assignment_id = ?", (assignment_id,))
        self._conn.execute("DELETE FROM index_urls WHERE assignment_id = ?", (assignment_id,))
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
            "SELECT * FROM sources WHERE assignment_id = ? ORDER BY times_seen DESC, name",
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

    def finish_run(
        self, run_id: int, scout_count: int, findings_count: int, cost_usd: float = 0.0
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, scout_count = ?, findings_count = ?, "
            "cost_usd = ? WHERE id = ?",
            (_now(), scout_count, findings_count, cost_usd, run_id),
        )
        self._conn.commit()

    def list_runs(self, assignment_id: str, limit: int = 10) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE assignment_id = ? ORDER BY id DESC LIMIT ?",
            (assignment_id, limit),
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def total_cost(self, assignment_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM runs WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()
        return float(row["total"])

    # -- findings ------------------------------------------------------------

    def latest_findings(self, assignment_id: str) -> list[Finding]:
        """One row per distinct listing: the most recent sighting of each.

        Everything that reasons about the current market goes through here.
        Reading the raw findings table instead would count a listing once per
        run it survived, so a stale listing nobody wants would outvote a
        genuinely rare cheap one purely by being re-seen.
        """
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE id IN ("
            "  SELECT MAX(id) FROM findings WHERE assignment_id = ? GROUP BY dedup_key"
            ") ORDER BY id DESC",
            (assignment_id,),
        ).fetchall()
        return [_row_to_finding(row) for row in rows]

    def price_history(self, assignment_id: str) -> dict[str | None, list[float]]:
        """Current asking prices, one per distinct listing, grouped by currency.

        Grouped rather than pooled because the numbers are not comparable
        across currencies: 3000 RSD against 30 EUR pooled as bare floats makes
        every dinar-priced listing look like the bargain of the year. A
        finding is only ever scored against others quoted in its own currency.
        """
        history: dict[str | None, list[float]] = {}
        for f in self.latest_findings(assignment_id):
            if f.price is not None:
                history.setdefault(f.currency, []).append(f.price)
        return history

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
        *,
        reference_price: float | None = None,
        reference_n: int | None = None,
        score_basis: str | None = None,
        peers: Sequence[tuple[str, float]] = (),
    ) -> Finding:
        cursor = self._conn.execute(
            "INSERT INTO findings (assignment_id, run_id, source, title, price, currency, "
            "url, dedup_key, score, is_new, found_at, reference_price, reference_n, "
            "score_basis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                reference_price,
                reference_n,
                score_basis,
            ),
        )
        if peers and cursor.lastrowid is not None:
            self._conn.executemany(
                "INSERT OR REPLACE INTO finding_comparables "
                "(finding_id, peer_dedup_key, similarity) VALUES (?, ?, ?)",
                [(cursor.lastrowid, key, sim) for key, sim in peers],
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

    def comparables(self, finding_id: int) -> list[tuple[str, float]]:
        """The peer listings a finding's reference price was drawn from.

        This is the evidence behind "why was this recommended": not a
        template, the actual other listings of the same thing and what they
        were asking.
        """
        rows = self._conn.execute(
            "SELECT peer_dedup_key, similarity FROM finding_comparables "
            "WHERE finding_id = ? ORDER BY similarity DESC",
            (finding_id,),
        ).fetchall()
        return [(row["peer_dedup_key"], row["similarity"]) for row in rows]

    def best_findings(self, assignment_id: str, limit: int = 5) -> list[Finding]:
        """Top listings by score, one entry per listing.

        Ranking the raw table would fill the whole list with repeat sightings
        of a single good listing.
        """
        ranked = sorted(
            self.latest_findings(assignment_id),
            key=lambda f: (f.score if f.score is not None else -1.0),
            reverse=True,
        )
        return ranked[:limit]


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
        market=row["market"],
        notify_agent=row["notify_agent"],
        wake_backend=row["wake_backend"],
        wake_target=row["wake_target"],
        wake_on=row["wake_on"],
        resume_job_id=row["resume_job_id"],
        runs_count=row["runs_count"],
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


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        assignment_id=row["assignment_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        scout_count=row["scout_count"],
        findings_count=row["findings_count"],
        cost_usd=row["cost_usd"],
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
        reference_price=row["reference_price"],
        reference_n=row["reference_n"],
        score_basis=row["score_basis"],
    )
