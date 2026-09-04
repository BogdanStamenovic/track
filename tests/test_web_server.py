from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
import web_support

from track.web.server import Config, free_port, gaps_for, serve


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    return web_support.seed_legacy(tmp_path)


@pytest.fixture
def future_db(tmp_path: Path) -> Path:
    return web_support.seed_future(tmp_path)


@pytest.fixture
def live(request: pytest.FixtureRequest) -> Iterator[tuple[str, Path]]:
    db: Path = request.getfixturevalue(request.param)
    port = free_port()
    servers = serve(Config(db_path=db, log=lambda _: None), ["127.0.0.1"], port)
    try:
        yield f"http://127.0.0.1:{port}", db
    finally:
        for server in servers:
            threading.Thread(target=server.shutdown).start()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@pytest.mark.parametrize("live", ["legacy_db"], indirect=True)
def test_index_and_assignment_render(live: tuple[str, Path]) -> None:
    base, _ = live
    code, html = get(base + "/")
    assert code == 200
    assert 'href="/a/aa11"' in html

    code, html = get(base + "/a/aa11")
    assert code == 200
    assert "Widget 3000" in html
    assert "blank on every card" in html


@pytest.mark.parametrize("live", ["legacy_db"], indirect=True)
def test_filters_and_sorts_over_http(live: tuple[str, Path]) -> None:
    base, _ = live
    _, html = get(base + "/a/aa11?q=mystery")
    assert "Mystery Box" in html and "Widget 3000" not in html

    _, cheap = get(base + "/a/aa11?sort=cheap")
    assert cheap.index("Widget 1000") < cheap.index("Widget 3000")

    _, bogus = get(base + "/a/aa11?sort=banana")
    assert bogus.count("chip on") == 1  # falls back to the default sort


@pytest.mark.parametrize("live", ["future_db"], indirect=True)
def test_dead_listings_are_hidden_by_default_and_shown_on_request(
    live: tuple[str, Path],
) -> None:

    base, _ = live
    _, hidden = get(base + "/a/aa11")
    assert "Widget 1000" not in hidden
    assert "2 of 3 listings" in hidden

    _, shown = get(base + "/a/aa11?dead=1")
    assert "Widget 1000" in shown
    assert "card dead" in shown


@pytest.mark.parametrize("live", ["legacy_db"], indirect=True)
def test_unknown_paths_and_health(live: tuple[str, Path]) -> None:
    base, _ = live
    assert get(base + "/healthz") == (200, "ok\n")
    assert get(base + "/a/nosuch")[0] == 404
    assert get(base + "/wat")[0] == 404


def test_gaps_are_empty_once_every_column_lands(future_db: Path) -> None:
    from track.web.data import connect, read_schema

    conn = connect(future_db)
    assert gaps_for(read_schema(conn)) == []
    conn.close()


def test_serving_a_deleted_database_fails_cleanly(tmp_path: Path) -> None:
    """A database that disappears must give a 503 page, not a stack trace."""
    port = free_port()
    servers = serve(Config(db_path=tmp_path / "gone.db", log=lambda _: None),
                    ["127.0.0.1"], port)
    try:
        code, html = get(f"http://127.0.0.1:{port}/")
        assert code == 503
        assert "no database" in html
    finally:
        for server in servers:
            threading.Thread(target=server.shutdown).start()


def test_a_host_that_cannot_bind_is_retried_not_fatal(legacy_db: Path) -> None:
    """One dead address must not stop the viewer, and must not be forgotten."""
    port = free_port()
    logged: list[str] = []
    servers = serve(
        Config(db_path=legacy_db, log=logged.append),
        ["127.0.0.1", "203.0.113.7"],  # TEST-NET-3: never local
        port,
        retry_seconds=0,
    )
    try:
        assert len(servers) == 1
        assert any("cannot bind" in line for line in logged)
        assert get(f"http://127.0.0.1:{port}/healthz") == (200, "ok\n")
    finally:
        for server in servers:
            threading.Thread(target=server.shutdown).start()


def test_no_bindable_address_is_an_error(legacy_db: Path) -> None:
    from track.web.data import WebError

    port = free_port()
    with pytest.raises(WebError, match="could not bind"):
        serve(Config(db_path=legacy_db, log=lambda _: None), ["203.0.113.7"], port)


def test_discovered_address_is_bound_when_it_appears(legacy_db: Path) -> None:
    port = free_port()
    servers = serve(
        Config(db_path=legacy_db, log=lambda _: None),
        ["127.0.0.1"],
        port,
        retry_seconds=0,
        discover=lambda: "127.0.0.2",
    )
    try:
        assert len(servers) == 1  # retry disabled, so only the initial bind
    finally:
        for server in servers:
            threading.Thread(target=server.shutdown).start()


def test_a_column_that_exists_but_is_empty_says_so_differently(future_db: Path) -> None:
    """A gap and an unfilled column are different facts and read differently."""
    import sqlite3

    from track.web.data import connect, load_listings, read_schema
    from track.web.server import unfilled_for

    conn = sqlite3.connect(future_db)
    conn.execute("UPDATE findings SET rationale = NULL, product_year = NULL")
    conn.commit()
    conn.close()

    ro = connect(future_db)
    schema = read_schema(ro)
    listings = load_listings(ro, schema, "aa11")
    ro.close()

    unfilled = unfilled_for(schema, listings)
    assert "why it was recommended" in unfilled
    assert "the product's model year" in unfilled
    assert gaps_for(schema) == []  # the columns exist, so nothing is missing


def test_reason_boxes_are_dropped_while_nothing_has_a_reason(future_db: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(future_db)
    conn.execute("UPDATE findings SET rationale = NULL")
    conn.commit()
    conn.close()

    port = free_port()
    servers = serve(Config(db_path=future_db, log=lambda _: None), ["127.0.0.1"], port)
    try:
        _, html = get(f"http://127.0.0.1:{port}/a/aa11?dead=1")
        assert "not captured yet" in html
        assert "no reason recorded" not in html
    finally:
        for server in servers:
            threading.Thread(target=server.shutdown).start()
