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
from .models import Assignment, Finding, ListingStatus, Run, Source
from .scoring import dedup_key as _dedup_key
from .scoring import index_url_bases as _index_url_bases
from .scoring import url_basis as _url_basis

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

CREATE TABLE IF NOT EXISTS listing_status (
    assignment_id   TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    check_failures  INTEGER NOT NULL DEFAULT 0,
    last_check_note TEXT,
    retired_at      TEXT,
    retired_reason  TEXT,
    retired_note    TEXT,
    superseded_by   TEXT,
    PRIMARY KEY (assignment_id, dedup_key)
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

-- One row per distinct listing: its most recent sighting, joined to the
-- state the reaper keeps. This is the contract anything outside track reads
-- -- the tables under it are free to change shape, this is not. `f.*` is
-- resolved when the view is queried rather than when it is created, so
-- columns added to findings appear here without the view being rebuilt.
CREATE VIEW IF NOT EXISTS listings_current AS
SELECT f.*,
       s.first_seen_at, s.last_seen_at, s.times_seen,
       s.last_checked_at, s.check_failures, s.last_check_note,
       s.retired_at, s.retired_reason, s.retired_note, s.superseded_by
FROM findings f
JOIN listing_status s
  ON s.assignment_id = f.assignment_id AND s.dedup_key = f.dedup_key
WHERE f.id IN (SELECT MAX(id) FROM findings GROUP BY assignment_id, dedup_key);
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
    ("assignments", "check_at", "TEXT"),
    ("assignments", "check_at_rationale", "TEXT"),
    ("assignments", "check_at_source", "TEXT"),
    ("assignments", "poweroff_after", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "cost_usd", "REAL NOT NULL DEFAULT 0"),
    ("findings", "reference_price", "REAL"),
    ("findings", "reference_n", "INTEGER"),
    ("findings", "score_basis", "TEXT"),
    ("findings", "rationale", "TEXT"),
    ("findings", "condition", "TEXT"),
    ("findings", "listing_posted_at", "TEXT"),
    ("findings", "listing_age_days", "REAL"),
    ("findings", "product_year", "INTEGER"),
    ("listing_status", "last_check_note", "TEXT"),
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
        self._backfill_listing_status()
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

        Version 2 also dropped the source name from URL-keyed identity, which
        merged five listings back together that had split when a scout
        renamed the site between runs.
        """
        name = "repair_dedup_keys_v2"
        if self._applied(name):
            return
        rows = self._conn.execute(
            "SELECT id, assignment_id, run_id, source, title, url, dedup_key FROM findings"
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
            moved: dict[tuple[str, str], str] = {}
            for row in rows:
                bases = discovered.get(row["assignment_id"], set())
                key = _dedup_key(row["source"], row["title"], row["url"], bases)
                if key != row["dedup_key"]:
                    moved[(row["assignment_id"], row["dedup_key"])] = key
                    self._conn.execute(
                        "UPDATE findings SET dedup_key = ? WHERE id = ?", (key, row["id"])
                    )
            if moved:
                self._remap_listing_status(moved)
        self._mark_applied(name)

    def _remap_listing_status(self, moved: dict[tuple[str, str], str]) -> None:
        """Follow the listings whose keys the repair just changed.

        `listing_status` is derived from `findings` by key, so rewriting keys
        without this strands every status row on a key nothing points at any
        more. It happened: 33 of 42 listings lost their status row and became
        invisible to the reaper, which then had nine listings to re-check
        instead of forty-two and no error anywhere.

        The counting columns are re-derived from the rows themselves, since
        merged listings need their sightings re-added rather than picked from
        one side. The reaper's verdicts cannot be re-derived, so they are
        carried across by hand.
        """
        keep = {
            (row["assignment_id"], row["dedup_key"]): row
            for row in self._conn.execute(
                "SELECT * FROM listing_status WHERE retired_at IS NOT NULL "
                "OR check_failures > 0 OR last_checked_at IS NOT NULL"
            ).fetchall()
        }
        # `last_check_note` predates nothing, but a database opened by an
        # older build will not have the column; ADD COLUMN has already run by
        # the time any of this does.
        self._conn.execute("DELETE FROM listing_status")
        self._rebuild_listing_status()
        for (assignment_id, old_key), row in keep.items():
            new_key = moved.get((assignment_id, old_key), old_key)
            self._conn.execute(
                "UPDATE listing_status SET last_checked_at = ?, check_failures = ?, "
                "last_check_note = ?, retired_at = ?, retired_reason = ?, retired_note = ?, "
                "superseded_by = ? WHERE assignment_id = ? AND dedup_key = ?",
                (
                    row["last_checked_at"],
                    row["check_failures"],
                    row["last_check_note"],
                    row["retired_at"],
                    row["retired_reason"],
                    row["retired_note"],
                    moved.get((assignment_id, row["superseded_by"] or ""), row["superseded_by"]),
                    assignment_id,
                    new_key,
                ),
            )

    def _rebuild_listing_status(self) -> None:
        """Derive first seen, last seen and sighting counts from the findings.

        Also drops status rows nothing points at any more. Those can only
        come from a key rewrite -- findings themselves are never deleted, so
        a retired listing always keeps the rows that anchor its status -- and
        left in place they are cruft that reads as live listings to anything
        querying the table directly rather than through the view.
        """
        self._conn.execute(
            "DELETE FROM listing_status WHERE NOT EXISTS ("
            "  SELECT 1 FROM findings f WHERE f.assignment_id = listing_status.assignment_id"
            "    AND f.dedup_key = listing_status.dedup_key)"
        )
        self._conn.execute(
            "INSERT INTO listing_status "
            "(assignment_id, dedup_key, first_seen_at, last_seen_at, times_seen) "
            "SELECT assignment_id, dedup_key, MIN(found_at), MAX(found_at), COUNT(*) "
            "FROM findings GROUP BY assignment_id, dedup_key "
            "ON CONFLICT(assignment_id, dedup_key) DO NOTHING"
        )

    def _backfill_listing_status(self) -> None:
        """Derive first/last seen and sighting counts from existing findings.

        Runs after the dedup repair, never before: the repair changes which
        rows belong to which listing, and a status table built on the old
        keys would describe listings that no longer exist. "How old is this
        listing" is answerable for every row already on record because
        `found_at` was always there -- it just had nowhere to be read from.
        """
        name = "backfill_listing_status_v1"
        if self._applied(name):
            # Still cheap and still idempotent, and it has to run anyway: a
            # key repair in the same open can strand rows, and a marker set
            # by an earlier version of the schema must not leave a listing
            # without a status row.
            self._rebuild_listing_status()
            return
        self._rebuild_listing_status()
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
        check_at: str | None = None,
        check_at_rationale: str | None = None,
        check_at_source: str | None = None,
        poweroff_after: bool = False,
    ) -> Assignment:
        assignment_id = uuid.uuid4().hex[:8]
        self._conn.execute(
            "INSERT INTO assignments (id, text, interval_seconds, status, max_price, "
            "created_at, market, notify_agent, wake_backend, wake_target, wake_on, "
            "check_at, check_at_rationale, check_at_source, poweroff_after) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                check_at,
                check_at_rationale,
                check_at_source,
                int(poweroff_after),
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

    def slot_members(self, check_at: str) -> list[Assignment]:
        """Active assignments that share one daily check time.

        Ordered by creation, so a slot's runs happen in a stable order rather
        than whatever sqlite feels like -- an unattended sequence that
        reorders itself between mornings is one nobody can reason about.
        """
        rows = self._conn.execute(
            "SELECT * FROM assignments WHERE check_at = ? AND status = 'active' "
            "ORDER BY created_at",
            (check_at,),
        ).fetchall()
        return [_row_to_assignment(row) for row in rows]

    def active_slots(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT check_at FROM assignments "
            "WHERE check_at IS NOT NULL AND status = 'active' ORDER BY check_at"
        ).fetchall()
        return [row["check_at"] for row in rows]

    def set_check_at(
        self,
        assignment_id: str,
        check_at: str | None,
        *,
        rationale: str | None = None,
        source: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE assignments SET check_at = ?, check_at_rationale = ?, "
            "check_at_source = ? WHERE id = ?",
            (check_at, rationale, source, assignment_id),
        )
        self._conn.commit()

    def set_poweroff_after(self, assignment_id: str, poweroff_after: bool) -> None:
        self._conn.execute(
            "UPDATE assignments SET poweroff_after = ? WHERE id = ?",
            (int(poweroff_after), assignment_id),
        )
        self._conn.commit()

    def set_wake_config(
        self,
        assignment_id: str,
        *,
        wake_backend: str | None = None,
        wake_target: str | None = None,
        wake_on: str | None = None,
    ) -> None:
        """Change how this assignment's machine is woken, field by field.

        Each field is left alone when not given rather than defaulted, so
        setting one cannot silently clear another -- a reschedule that
        changed the backend and wiped the MAC with it would arm a wol task
        with nothing to wake.
        """
        updates = {
            "wake_backend": wake_backend,
            "wake_target": wake_target,
            "wake_on": wake_on,
        }
        given = {name: value for name, value in updates.items() if value is not None}
        if not given:
            return
        assignments = ", ".join(f"{name} = ?" for name in given)
        self._conn.execute(
            f"UPDATE assignments SET {assignments} WHERE id = ?",
            (*given.values(), assignment_id),
        )
        self._conn.commit()

    def set_interval(self, assignment_id: str, interval_seconds: int) -> None:
        self._conn.execute(
            "UPDATE assignments SET interval_seconds = ? WHERE id = ?",
            (interval_seconds, assignment_id),
        )
        self._conn.commit()

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
        self._conn.execute(
            "DELETE FROM listing_status WHERE assignment_id = ?", (assignment_id,)
        )
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
        rationale: str | None = None,
        condition: str | None = None,
        listing_posted_at: str | None = None,
        listing_age_days: float | None = None,
        product_year: int | None = None,
    ) -> Finding:
        cursor = self._conn.execute(
            "INSERT INTO findings (assignment_id, run_id, source, title, price, currency, "
            "url, dedup_key, score, is_new, found_at, reference_price, reference_n, "
            "score_basis, rationale, condition, listing_posted_at, listing_age_days, "
            "product_year) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                rationale,
                condition,
                listing_posted_at,
                listing_age_days,
                product_year,
            ),
        )
        self._touch_listing(assignment_id, dedup_key)
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

    def _touch_listing(self, assignment_id: str, dedup_key: str) -> None:
        """Record that this listing was seen again, right now.

        A sighting also un-retires: a listing the reaper had marked gone that
        turns up alive in a later run is alive, and leaving the marking on it
        would hide a real listing behind a stale verdict. The failure counter
        resets for the same reason.
        """
        now = _now()
        self._conn.execute(
            "INSERT INTO listing_status (assignment_id, dedup_key, first_seen_at, "
            "last_seen_at, times_seen) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(assignment_id, dedup_key) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, times_seen = times_seen + 1, "
            "check_failures = 0, retired_at = NULL, retired_reason = NULL, "
            "retired_note = NULL, superseded_by = NULL",
            (assignment_id, dedup_key, now, now),
        )

    def listing_status(self, assignment_id: str, dedup_key: str) -> ListingStatus | None:
        row = self._conn.execute(
            "SELECT * FROM listing_status WHERE assignment_id = ? AND dedup_key = ?",
            (assignment_id, dedup_key),
        ).fetchone()
        return _row_to_listing_status(row) if row else None

    def live_listings(self, assignment_id: str) -> list[Finding]:
        """Latest sighting of every listing the reaper has not retired."""
        retired = {
            row["dedup_key"]
            for row in self._conn.execute(
                "SELECT dedup_key FROM listing_status WHERE assignment_id = ? "
                "AND retired_at IS NOT NULL",
                (assignment_id,),
            ).fetchall()
        }
        return [f for f in self.latest_findings(assignment_id) if f.dedup_key not in retired]

    def listings_due_for_check(
        self, assignment_id: str, *, exclude: set[str], limit: int
    ) -> list[Finding]:
        """Live listings this run did not see, least recently checked first.

        Round-robin rather than newest-first, so a large back catalogue is
        worked through over successive runs instead of the same few listings
        being re-checked forever. Listings the run *did* see are excluded by
        the caller: a listing a scout just found is alive, and re-fetching it
        to confirm that would be the most expensive way to learn nothing.
        """
        rows = self._conn.execute(
            "SELECT f.* FROM findings f JOIN listing_status s "
            "  ON s.assignment_id = f.assignment_id AND s.dedup_key = f.dedup_key "
            "WHERE f.assignment_id = ? AND s.retired_at IS NULL AND f.url IS NOT NULL "
            "  AND f.id IN (SELECT MAX(id) FROM findings WHERE assignment_id = ? "
            "               GROUP BY dedup_key) "
            "ORDER BY COALESCE(s.last_checked_at, '') ASC, f.id ASC",
            (assignment_id, assignment_id),
        ).fetchall()
        # A listing whose only URL is a search or category page cannot be
        # re-checked by fetching it: the page will render perfectly whether or
        # not that particular item is still on it, so a "live" answer would
        # mean nothing and a "gone" answer would retire every listing behind
        # that URL at once. Twelve due listings collapsed to seven distinct
        # URLs before this, and the five that shared one were dropped without
        # a word.
        index_bases = self.index_url_bases(assignment_id)
        return [
            finding
            for row in rows
            if (finding := _row_to_finding(row)).dedup_key not in exclude
            and finding.url is not None
            and _url_basis(finding.url) not in index_bases
        ][:limit]

    def record_check(
        self, assignment_id: str, dedup_key: str, *, conclusive: bool, note: str | None
    ) -> int:
        """Note that a listing was re-checked. Returns the failure count after.

        `conclusive` is False for a check that could not establish anything --
        a timeout, or a site that refused to talk to us. Those accumulate
        toward retirement only when they are *inconclusive*; a site-level
        block is recorded and explicitly does not count, because a 403 is
        evidence about the site and none at all about the listing.
        """
        self._conn.execute(
            "UPDATE listing_status SET last_checked_at = ?, last_check_note = ?, "
            "check_failures = CASE WHEN ? THEN 0 ELSE check_failures + 1 END "
            "WHERE assignment_id = ? AND dedup_key = ?",
            (_now(), note, conclusive, assignment_id, dedup_key),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT check_failures FROM listing_status WHERE assignment_id = ? AND dedup_key = ?",
            (assignment_id, dedup_key),
        ).fetchone()
        return int(row["check_failures"]) if row else 0

    def record_block(self, assignment_id: str, dedup_key: str, note: str | None) -> None:
        """A site refused the check. Timestamped, noted, and never counted."""
        self._conn.execute(
            "UPDATE listing_status SET last_checked_at = ?, last_check_note = ? "
            "WHERE assignment_id = ? AND dedup_key = ?",
            (_now(), note, assignment_id, dedup_key),
        )
        self._conn.commit()

    def retire(
        self,
        assignment_id: str,
        dedup_key: str,
        *,
        reason: str,
        note: str | None = None,
        superseded_by: str | None = None,
    ) -> None:
        """Mark a listing retired. Never deletes anything.

        Every sighting the listing ever had stays exactly where it was, which
        is what makes "what did that cost in July" answerable after the advert
        is gone. Retirement is a fact about now, and a later sighting clears
        it (see `_touch_listing`).
        """
        self._conn.execute(
            "UPDATE listing_status SET retired_at = ?, retired_reason = ?, "
            "retired_note = COALESCE(?, retired_note), superseded_by = ? "
            "WHERE assignment_id = ? AND dedup_key = ?",
            (_now(), reason, note, superseded_by, assignment_id, dedup_key),
        )
        self._conn.commit()

    def retired_listings(self, assignment_id: str) -> list[ListingStatus]:
        rows = self._conn.execute(
            "SELECT * FROM listing_status WHERE assignment_id = ? AND retired_at IS NOT NULL "
            "ORDER BY retired_at DESC",
            (assignment_id,),
        ).fetchall()
        return [_row_to_listing_status(row) for row in rows]

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


def _row_to_listing_status(row: sqlite3.Row) -> ListingStatus:
    return ListingStatus(
        assignment_id=row["assignment_id"],
        dedup_key=row["dedup_key"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        times_seen=row["times_seen"],
        last_checked_at=row["last_checked_at"],
        check_failures=row["check_failures"],
        last_check_note=row["last_check_note"],
        retired_at=row["retired_at"],
        retired_reason=row["retired_reason"],
        retired_note=row["retired_note"],
        superseded_by=row["superseded_by"],
    )


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
        check_at=row["check_at"],
        check_at_rationale=row["check_at_rationale"],
        check_at_source=row["check_at_source"],
        poweroff_after=bool(row["poweroff_after"]),
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
        rationale=row["rationale"],
        condition=row["condition"],
        listing_posted_at=row["listing_posted_at"],
        listing_age_days=row["listing_age_days"],
        product_year=row["product_year"],
    )
