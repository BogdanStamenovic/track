from __future__ import annotations

from pathlib import Path

import pytest
import web_support

from track.web.data import connect, load_assignments, load_listings, read_schema
from track.web.render import brief, render_assignment, render_index, render_listing, short_label
from track.web.server import gaps_for


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    return web_support.seed_legacy(tmp_path)


@pytest.fixture
def future_db(tmp_path: Path) -> Path:
    return web_support.seed_future(tmp_path)



def _ctx(path: Path):
    conn = connect(path)
    schema = read_schema(conn)
    listings = load_listings(conn, schema, "aa11")
    assignment = load_assignments(conn)[0]
    conn.close()
    return assignment, listings, schema


def test_short_label_cuts_a_paragraph_brief() -> None:
    label = short_label("a test hunt: with a long brief that goes on and on and on")
    assert label == "a test hunt"
    assert len(short_label("x" * 300)) <= 81


def test_brief_is_omitted_when_it_adds_nothing() -> None:
    assert brief("short") == ""
    assert "full brief" in brief("a test hunt: with a long brief that goes on")


def test_missing_values_are_stated_not_invented(legacy_db: Path) -> None:
    _, listings, _ = _ctx(legacy_db)
    mystery = next(x for x in listings if x.dedup_key == "k2")
    html = render_listing(mystery, has_reason=False)
    assert "no price listed" in html
    assert "not scored" in html
    assert "no link recorded" in html
    assert "Mystery Box" in html


def test_reason_box_is_dropped_when_the_column_does_not_exist(legacy_db: Path) -> None:
    _, listings, _ = _ctx(legacy_db)
    html = render_listing(listings[0], has_reason=False)
    assert "no reason recorded" not in html
    html = render_listing(listings[0], has_reason=True)
    assert "no reason recorded" in html


def test_dead_listing_is_visibly_dead_and_says_why(future_db: Path) -> None:
    _, listings, _ = _ctx(future_db)
    dead = next(x for x in listings if x.dedup_key == "k3")
    html = render_listing(dead)
    assert 'class="card dead"' in html
    assert "badge dead" in html
    assert "gone" in html
    assert "the seller marked it sold" in html


def test_an_unreachable_listing_is_flagged_but_not_struck_out(future_db: Path) -> None:
    _, listings, _ = _ctx(future_db)
    blocked = next(x for x in listings if x.dedup_key == "k2")
    html = render_listing(blocked)
    assert 'class="card dead"' not in html, "a failed check must not read as sold"
    assert "badge warn" in html
    assert "blocked: 403 from the source" in html
    assert "not proof a listing is gone" in html


def test_titles_and_reasons_are_escaped(future_db: Path) -> None:
    _, listings, _ = _ctx(future_db)
    item = listings[0]
    item.title = '<script>alert(1)</script>'
    item.extras["reason"] = '"><img onerror=x>'
    html = render_listing(item)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "onerror=x>" not in html


def test_assignment_page_has_the_controls_and_the_gap_banner(legacy_db: Path) -> None:
    assignment, listings, schema = _ctx(legacy_db)
    html = render_assignment(
        assignment, listings, total=len(listings), sort="score", query="",
        show_dead=False, dead_known=False, has_reason=False, gaps=gaps_for(schema),
    )
    assert "why it was recommended" in html  # the folded note names the gap
    assert "4 fields are blank on every card" in html
    assert "sort=cheap" in html and "sort=new" in html
    assert "hiding gone" not in html  # no dead column -> no dead filter offered
    assert 'name="q"' in html


def test_dead_filter_appears_once_the_column_exists(future_db: Path) -> None:
    assignment, listings, schema = _ctx(future_db)
    html = render_assignment(
        assignment, listings, total=len(listings), sort="score", query="",
        show_dead=False, dead_known=True, has_reason=True, gaps=gaps_for(schema),
    )
    assert "hiding gone" in html
    assert "blank on every card" not in html  # nothing missing any more


def test_index_lists_assignments(legacy_db: Path) -> None:
    conn = connect(legacy_db)
    assignments = load_assignments(conn)
    conn.close()
    html = render_index(assignments)
    assert 'href="/a/aa11"' in html
    assert "a test hunt" in html


def test_score_basis_is_shown_when_track_records_it(legacy_db: Path) -> None:
    """The numeric half of "why": what the score was measured against."""
    _, listings, _ = _ctx(legacy_db)
    item = next(x for x in listings if x.dedup_key == "k1")
    item.extras.update({"reference_price": 610.0, "reference_n": 7,
                        "score_basis": "median of comparables"})
    html = render_listing(item)
    assert "vs 7 comparables at 610 EUR" in html
    assert "median of comparables" in html


def test_a_single_comparable_is_named_as_one(legacy_db: Path) -> None:
    _, listings, _ = _ctx(legacy_db)
    item = next(x for x in listings if x.dedup_key == "k1")
    item.extras.update({"reference_price": 610.0, "reference_n": 1})
    assert "vs 1 comparable at 610 EUR" in render_listing(item)


def test_score_basis_is_absent_when_track_records_nothing(legacy_db: Path) -> None:
    _, listings, _ = _ctx(legacy_db)
    html = render_listing(next(x for x in listings if x.dedup_key == "k1"))
    assert "vs a reference" not in html


def test_listing_age_in_days_reads_as_a_sentence(legacy_db: Path) -> None:
    from track.web.render import _days

    assert _days(27.0) == "27 days"
    assert _days(1) == "1 day"
    assert _days(2.5) == "2.5 days"
    assert _days(0.25) == "6 hours"

    _, listings, _ = _ctx(legacy_db)
    item = listings[0]
    item.extras["listing_age_days"] = 27.0
    assert "listed 27 days ago" in render_listing(item)
