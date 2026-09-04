"""Read-only access to track's findings database.

Two things make this module more than a SELECT:

1. **It never writes and never locks.** track runs scouts against this same file
   while the viewer is open, so every connection is opened through a
   `file:...?mode=ro&immutable=0` URI with a short busy timeout. A viewer that
   took a write lock would stall a run.

2. **The schema is still moving.** `findings` today has no rationale, no listing
   age and no dead flag; track-core is adding them. Rather than guess at column
   names, this module inspects the live table and resolves each *logical* field
   against a list of plausible names. A field that resolves to nothing is
   reported as absent -- callers render "not recorded" and never invent a value.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "track" / "track.db"

#: Logical field -> column names it may appear under, best candidate first.
#: track-core owns the real names; when they land, the winning candidate is
#: whichever exists. Adding a name here is the whole cost of a schema change.
COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "reason": ("reason", "rationale", "why", "score_reason", "reason_text", "explanation"),
    "listed_at": ("listing_posted_at", "listed_at", "posted_at", "published_at",
                  "listing_date", "first_listed_at"),
    "listing_age_days": ("listing_age_days", "age_days", "listed_days_ago"),
    "model_year": ("product_year", "model_year", "release_year", "year"),
    "condition": ("condition", "item_condition", "state_of_goods"),
    # How the score was arrived at. Not "why it was recommended" in prose, but
    # the numeric half of the same question, and derived rather than invented.
    "score_basis": ("score_basis", "basis", "score_method", "scoring_basis"),
    "reference_price": ("reference_price", "ref_price", "comparable_price", "market_price"),
    "reference_n": ("reference_n", "reference_count", "n_comparables", "comparables"),
}

#: Columns read off `listing_status` when that table exists. track owns
#: retirement there rather than on `findings`, because a sighting row is a fact
#: about the past and retirement is a fact about now.
STATUS_COLUMNS = (
    "first_seen_at",
    "last_seen_at",
    "times_seen",
    "last_checked_at",
    "check_failures",
    "last_check_note",
    "retired_at",
    "retired_reason",
    "retired_note",
    "superseded_by",
)


class WebError(Exception):
    """Raised when track-web cannot read or interpret the database."""


@dataclass(frozen=True)
class Schema:
    """Which optional columns this database actually has."""

    columns: frozenset[str]
    resolved: dict[str, str | None]

    def col(self, logical: str) -> str | None:
        return self.resolved.get(logical)

    def has(self, logical: str) -> bool:
        return self.resolved.get(logical) is not None

    @property
    def missing(self) -> list[str]:
        return sorted(k for k, v in self.resolved.items() if v is None)

    @property
    def present(self) -> dict[str, str]:
        return {k: v for k, v in sorted(self.resolved.items()) if v is not None}


@dataclass
class Assignment:
    id: str
    text: str
    status: str
    market: str | None
    max_price: float | None
    interval_seconds: int | None
    created_at: str | None
    last_run_at: str | None
    next_run_at: str | None
    runs_count: int
    listing_count: int = 0
    dead_count: int = 0


@dataclass
class Listing:
    """One real-world listing, collapsed from every sighting of it.

    track appends a row per sighting, so the same product appears many times
    across runs. `first_seen_at` / `last_seen_at` / `times_seen` come from that
    history -- they describe when *track* saw the listing, which is not the same
    as when the seller posted it. Only `listed_at` means the latter, and only
    when the database actually carries it.
    """

    dedup_key: str
    assignment_id: str
    title: str
    source: str
    url: str | None
    price: float | None
    currency: str | None
    price_stale: bool
    score: float | None
    score_stale: bool
    first_seen_at: str | None
    last_seen_at: str | None
    times_seen: int
    latest_id: int
    latest_run_id: int
    is_new: bool
    extras: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str | None:
        value = self.extras.get("reason")
        return str(value) if value not in (None, "") else None

    @property
    def model_year(self) -> Any:
        return self.extras.get("model_year")

    @property
    def listed_at(self) -> Any:
        return self.extras.get("listed_at")

    @property
    def condition(self) -> str | None:
        value = self.extras.get("condition")
        return str(value) if value not in (None, "") else None

    @property
    def score_basis(self) -> str | None:
        value = self.extras.get("score_basis")
        return str(value) if value not in (None, "") else None

    @property
    def dead(self) -> bool | None:
        """True/False when track says so, None when it has not said.

        Deliberately does NOT infer death from "absent in the latest run".
        Retiring a listing is the reaper's job; guessing it here would put a
        strikethrough on a live product because a scout got rate-limited.
        """
        if not self.status:
            return None
        return self.status.get("retired_at") not in (None, "")

    @property
    def retired_reason(self) -> str | None:
        """"gone" or "superseded" -- track's own vocabulary, not ours."""
        value = self.status.get("retired_reason")
        return str(value) if value not in (None, "") else None

    @property
    def retired_note(self) -> str | None:
        value = self.status.get("retired_note")
        return str(value) if value not in (None, "") else None

    @property
    def superseded_by(self) -> str | None:
        value = self.status.get("superseded_by")
        return str(value) if value not in (None, "") else None

    @property
    def unverified(self) -> str | None:
        """A live listing that track could not reach, and what stopped it.

        Kept strictly apart from `dead`. track's own model comments make the
        same point: a field named for retirement that holds the outcome of a
        check on a live listing gets read wrong. A block is not a sold GPU.
        """
        if self.dead:
            return None
        failures = self.status.get("check_failures") or 0
        if not failures:
            return None
        note = self.status.get("last_check_note")
        if note:
            return str(note)
        n = int(failures)
        return f"{n} failed check" + ("" if n == 1 else "s")

    @property
    def age_days(self) -> float | None:
        """Days since track first saw this listing."""
        return _days_since(self.first_seen_at)


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database read-only. Raises WebError if that is impossible."""
    path = Path(path)
    if not path.exists():
        raise WebError(f"no database at {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
    except sqlite3.Error as exc:
        raise WebError(f"cannot open {path} read-only: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def read_schema(conn: sqlite3.Connection) -> Schema:
    try:
        rows = conn.execute("PRAGMA table_info(findings)").fetchall()
    except sqlite3.Error as exc:
        raise WebError(f"cannot inspect findings table: {exc}") from exc
    if not rows:
        raise WebError("this database has no findings table")
    columns = frozenset(str(r["name"]) for r in rows)
    resolved: dict[str, str | None] = {}
    for logical, candidates in COLUMN_CANDIDATES.items():
        resolved[logical] = next((c for c in candidates if c in columns), None)
    return Schema(columns=columns, resolved=resolved)


def load_assignments(conn: sqlite3.Connection) -> list[Assignment]:
    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(assignments)")}

    def pick(name: str) -> str:
        return name if name in cols else "NULL"

    sql = (
        f"SELECT id, text, {pick('status')} AS status, {pick('market')} AS market, "
        f"{pick('max_price')} AS max_price, {pick('interval_seconds')} AS interval_seconds, "
        f"{pick('created_at')} AS created_at, {pick('last_run_at')} AS last_run_at, "
        f"{pick('next_run_at')} AS next_run_at, {pick('runs_count')} AS runs_count "
        "FROM assignments ORDER BY COALESCE(last_run_at, created_at) DESC, id"
    )
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise WebError(f"cannot read assignments: {exc}") from exc
    return [
        Assignment(
            id=str(r["id"]),
            text=str(r["text"] or ""),
            status=str(r["status"] or "unknown"),
            market=r["market"],
            max_price=r["max_price"],
            interval_seconds=r["interval_seconds"],
            created_at=r["created_at"],
            last_run_at=r["last_run_at"],
            next_run_at=r["next_run_at"],
            runs_count=int(r["runs_count"] or 0),
        )
        for r in rows
    ]


def has_status_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_status'"
    ).fetchone()
    return row is not None


def load_status(
    conn: sqlite3.Connection, assignment_id: str | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """Per-listing retirement state, keyed the same way listings are.

    Empty dict when the table does not exist, which is what makes `dead`
    return None rather than False on an older database.
    """
    if not has_status_table(conn):
        return {}
    have = {str(r["name"]) for r in conn.execute("PRAGMA table_info(listing_status)")}
    cols = [c for c in STATUS_COLUMNS if c in have]
    sql = f"SELECT assignment_id, dedup_key, {', '.join(cols)} FROM listing_status"
    params: list[Any] = []
    if assignment_id:
        sql += " WHERE assignment_id = ?"
        params.append(assignment_id)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise WebError(f"cannot read listing_status: {exc}") from exc
    return {
        (str(r["assignment_id"]), str(r["dedup_key"])): {c: r[c] for c in cols} for r in rows
    }


def load_listings(
    conn: sqlite3.Connection, schema: Schema, assignment_id: str | None = None
) -> list[Listing]:
    """Collapse every sighting into one Listing per (assignment, dedup_key).

    Grouping happens in Python rather than SQL because the merge is not a plain
    "latest row wins": a later sighting with a NULL price must not erase a price
    we actually observed earlier. The row count here is in the hundreds, so the
    cost of pulling it all is nil and the logic stays readable.
    """
    extra_cols = [c for c in schema.present.values()]
    select = ["id", "assignment_id", "run_id", "source", "title", "price", "currency",
              "url", "dedup_key", "score", "is_new", "found_at", *extra_cols]
    sql = f"SELECT {', '.join(select)} FROM findings"
    params: list[Any] = []
    if assignment_id:
        sql += " WHERE assignment_id = ?"
        params.append(assignment_id)
    sql += " ORDER BY id"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise WebError(f"cannot read findings: {exc}") from exc

    logical_for = {v: k for k, v in schema.present.items()}
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((str(row["assignment_id"]), str(row["dedup_key"])), []).append(row)

    status = load_status(conn, assignment_id)
    listings = [
        _collapse(sightings, logical_for, status.get(key, {}))
        for key, sightings in grouped.items()
    ]
    listings.sort(key=lambda listing: (listing.last_seen_at or "", listing.latest_id), reverse=True)
    return listings


def _collapse(
    sightings: list[sqlite3.Row], logical_for: dict[str, str], status: dict[str, Any]
) -> Listing:
    latest = sightings[-1]
    price_row = _latest_with(sightings, "price")
    score_row = _latest_with(sightings, "score")
    url_row = _latest_with(sightings, "url")
    found = [str(r["found_at"]) for r in sightings if r["found_at"]]

    extras: dict[str, Any] = {}
    for column, logical in logical_for.items():
        # Newest non-null wins: a field the reaper only fills in later must not be
        # hidden by an older sighting that predates the column existing.
        source_row = _latest_with(sightings, column) or latest
        extras[logical] = source_row[column]

    return Listing(
        dedup_key=str(latest["dedup_key"]),
        assignment_id=str(latest["assignment_id"]),
        title=str(latest["title"] or "(untitled)"),
        source=str(latest["source"] or "unknown"),
        url=(url_row["url"] if url_row else None),
        price=(price_row["price"] if price_row else None),
        currency=(price_row["currency"] if price_row else None),
        price_stale=bool(price_row is not None and price_row["id"] != latest["id"]),
        score=(score_row["score"] if score_row else None),
        score_stale=bool(score_row is not None and score_row["id"] != latest["id"]),
        # track's own counts win where it has them: it sees runs we do not,
        # and its first_seen_at survives rows this query did not fetch.
        first_seen_at=status.get("first_seen_at") or (min(found) if found else None),
        last_seen_at=status.get("last_seen_at") or (max(found) if found else None),
        times_seen=int(status.get("times_seen") or len(sightings)),
        latest_id=int(latest["id"]),
        latest_run_id=int(latest["run_id"]),
        is_new=bool(latest["is_new"]),
        extras=extras,
        status=status,
    )


def _latest_with(rows: Sequence[sqlite3.Row], column: str) -> sqlite3.Row | None:
    for row in reversed(rows):
        value = row[column]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return row
    return None


def annotate_counts(assignments: Iterable[Assignment], listings: Sequence[Listing]) -> None:
    for assignment in assignments:
        mine = [item for item in listings if item.assignment_id == assignment.id]
        assignment.listing_count = len(mine)
        assignment.dead_count = sum(1 for item in mine if item.dead)


SORTS = {
    "score": "best score",
    "new": "newest",
    "cheap": "cheapest",
    "age": "oldest sighting",
}


@dataclass
class Summary:
    """What `track-web info` reports: the shape of the database, not its contents."""

    db: str
    columns: list[str]
    resolved: dict[str, str | None]
    gaps: list[str]
    sightings: int
    listings: int
    assignments: list[Assignment]
    without_price: int
    without_score: int
    without_url: int
    without_reason: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "db": self.db,
            "columns": self.columns,
            "resolved": self.resolved,
            "gaps": self.gaps,
            "sightings": self.sightings,
            "listings": self.listings,
            "assignments": [
                {
                    "id": a.id,
                    "text": a.text,
                    "status": a.status,
                    "runs": a.runs_count,
                    "listings": a.listing_count,
                    "dead": a.dead_count,
                    "last_run_at": a.last_run_at,
                }
                for a in self.assignments
            ],
            "without_price": self.without_price,
            "without_score": self.without_score,
            "without_url": self.without_url,
            "without_reason": self.without_reason,
        }


def summarize(conn: sqlite3.Connection, path: Path, gaps: list[str]) -> Summary:
    schema = read_schema(conn)
    assignments = load_assignments(conn)
    listings = load_listings(conn, schema)
    annotate_counts(assignments, listings)
    sightings = int(conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0])
    return Summary(
        db=str(path),
        columns=sorted(schema.columns),
        resolved=dict(schema.resolved),
        gaps=gaps,
        sightings=sightings,
        listings=len(listings),
        assignments=assignments,
        without_price=sum(1 for x in listings if x.price is None),
        without_score=sum(1 for x in listings if x.score is None),
        without_url=sum(1 for x in listings if not x.url),
        without_reason=sum(1 for x in listings if not x.reason),
    )


def sort_listings(listings: list[Listing], sort: str) -> list[Listing]:
    """Order for display. NULLs always sink -- an unscored item is not a zero."""
    if sort == "new":
        key = lambda x: (x.last_seen_at or "", x.latest_id)
        return sorted(listings, key=key, reverse=True)
    if sort == "cheap":
        priced = [x for x in listings if x.price is not None]
        unpriced = [x for x in listings if x.price is None]
        return sorted(priced, key=lambda x: x.price or 0.0) + unpriced
    if sort == "age":
        dated = [x for x in listings if x.first_seen_at]
        undated = [x for x in listings if not x.first_seen_at]
        return sorted(dated, key=lambda x: x.first_seen_at or "") + undated
    scored = [x for x in listings if x.score is not None]
    unscored = [x for x in listings if x.score is None]
    scored.sort(key=lambda x: (x.score or 0.0, x.last_seen_at or ""), reverse=True)
    unscored.sort(key=lambda x: (x.last_seen_at or "", x.latest_id), reverse=True)
    return scored + unscored


def search(listings: Sequence[Listing], query: str) -> list[Listing]:
    needle = query.strip().lower()
    if not needle:
        return list(listings)
    terms = needle.split()
    out = []
    for item in listings:
        hay = " ".join(
            str(part).lower()
            for part in (
                item.title,
                item.source,
                item.reason or "",
                item.url or "",
                item.retired_note or "",
            )
        )
        if all(term in hay for term in terms):
            out.append(item)
    return out


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days_since(value: Any) -> float | None:
    parsed = parse_ts(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0
