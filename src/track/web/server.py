"""The HTTP layer: routing, request handling, and binding.

Every request opens its own read-only connection. That is deliberate rather than
wasteful: track adds rows and (right now) columns to this file while the viewer
is running, and a per-request connection picks up a schema change without a
restart. Opening a local sqlite file costs microseconds.
"""

from __future__ import annotations

import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from .data import (
    SORTS,
    Listing,
    Schema,
    WebError,
    annotate_counts,
    connect,
    has_status_table,
    load_assignments,
    load_listings,
    read_schema,
    search,
    sort_listings,
)
from .render import render_assignment, render_error, render_index

#: Logical column -> the thing he asked to see. Any concept with no column
#: behind it is announced on the page instead of being silently blank.
GAP_LABELS: list[tuple[tuple[str, ...], str]] = [
    (("reason",), "why it was recommended"),
    (("listed_at", "listing_age_days"), "when the listing was posted"),
    (("model_year",), "the product's model year"),
    (("condition",), "the item's condition"),
]


def gaps_for(schema: Schema) -> list[str]:
    """Concepts with no column behind them at all."""
    return [label for keys, label in GAP_LABELS if not any(schema.has(k) for k in keys)]


def unfilled_for(schema: Schema, listings: Sequence[Listing]) -> list[str]:
    """Concepts whose column exists but is empty on every listing here.

    Distinct from a gap and worth saying so: "track cannot record this" and
    "track can, but has not captured it on this assignment yet" are different
    facts, and only the second one resolves itself on the next run.
    """
    return [
        label
        for keys, label in GAP_LABELS
        if any(schema.has(k) for k in keys)
        and not any(
            item.extras.get(k) not in (None, "")
            for item in listings
            for k in keys
            if schema.has(k)
        )
    ]


@dataclass
class Config:
    db_path: Path
    log: Callable[[str], None]


def _handler(config: Config) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"track-web/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            config.log(f"{self.address_string()} {fmt % args}")

        def _send(self, body: str, code: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            params = parse_qs(parts.query)

            if path == "/healthz":
                self._send("ok\n", ctype="text/plain; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._send("", code=404, ctype="text/plain")
                return

            try:
                conn = connect(config.db_path)
            except WebError as exc:
                self._send(render_error(str(exc), 503), code=503)
                return
            try:
                self._route(conn, path, params)
            except WebError as exc:
                self._send(render_error(str(exc), 500), code=500)
            except sqlite3.Error as exc:
                self._send(render_error(f"database error: {exc}", 500), code=500)
            finally:
                conn.close()

        def _route(self, conn: sqlite3.Connection, path: str, params: dict[str, list[str]]) -> None:
            schema = read_schema(conn)
            gaps = gaps_for(schema)

            if path in ("/", ""):
                assignments = load_assignments(conn)
                annotate_counts(assignments, load_listings(conn, schema))
                self._send(render_index(assignments))
                return

            if path.startswith("/a/"):
                wanted = path[3:].strip("/")
                assignment = next(
                    (a for a in load_assignments(conn) if a.id == wanted), None
                )
                if assignment is None:
                    self._send(render_error(f"no assignment {wanted!r}", 404), code=404)
                    return

                listings = load_listings(conn, schema, assignment.id)
                annotate_counts([assignment], listings)
                unfilled = unfilled_for(schema, listings)
                any_reason = any(x.reason for x in listings)
                dead_known = has_status_table(conn)
                show_dead = params.get("dead", ["0"])[0] not in ("0", "", "no")
                total = len(listings)
                if dead_known and not show_dead:
                    listings = [x for x in listings if x.dead is not True]
                query = params.get("q", [""])[0]
                listings = search(listings, query)
                sort = params.get("sort", ["score"])[0]
                if sort not in SORTS:
                    sort = "score"
                self._send(
                    render_assignment(
                        assignment,
                        sort_listings(listings, sort),
                        total=total,
                        sort=sort,
                        query=query,
                        show_dead=show_dead,
                        dead_known=dead_known,
                        has_reason=schema.has("reason") and any_reason,
                        gaps=gaps,
                        unfilled=unfilled,
                    )
                )
                return

            self._send(render_error(f"no page at {path}", 404), code=404)

    return Handler


def tailnet_address() -> str | None:
    """This host's Tailscale IPv4, if it has one.

    Read off the interfaces rather than hardcoded, so the tool survives the
    address changing. 100.64.0.0/10 is the CGNAT range Tailscale allocates from.
    """
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        for token in line.split():
            if "/" in token and token.count(".") == 3:
                addr = token.split("/")[0]
                first, second = (int(x) for x in addr.split(".")[:2])
                if first == 100 and 64 <= second <= 127:
                    return addr
    return None


def _bind(config: Config, handler: type[BaseHTTPRequestHandler], host: str, port: int
          ) -> ThreadingHTTPServer | None:
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        config.log(f"cannot bind {_pretty(host)}:{port}: {exc}")
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config.log(f"listening on http://{_pretty(host)}:{port}")
    return server


def serve(
    config: Config,
    hosts: Sequence[str],
    port: int,
    *,
    retry_seconds: float = 300.0,
    discover: Callable[[], str | None] | None = None,
) -> list[ThreadingHTTPServer]:
    """Bind one server per address.

    Never 0.0.0.0 by default: this is a private view of his shopping, and a
    wildcard bind would put it on every network the box is attached to.

    Anything that fails to bind is retried in the background for `retry_seconds`.
    That exists for one concrete case: started as a boot-time user service, this
    can come up before tailscaled has an address, and binding localhost only
    would look like success while the phone silently could not reach it.
    """
    handler = _handler(config)
    bound: dict[str, ThreadingHTTPServer] = {}
    pending = []
    for host in hosts:
        server = _bind(config, handler, host, port)
        if server is not None:
            bound[host] = server
        else:
            pending.append(host)
    if not bound:
        raise WebError(f"could not bind any address on port {port}")

    if retry_seconds > 0 and (pending or discover):
        threading.Thread(
            target=_retry_binds,
            args=(config, handler, bound, pending, port, retry_seconds, discover),
            daemon=True,
        ).start()
    return list(bound.values())


def _retry_binds(
    config: Config,
    handler: type[BaseHTTPRequestHandler],
    bound: dict[str, ThreadingHTTPServer],
    pending: list[str],
    port: int,
    seconds: float,
    discover: Callable[[], str | None] | None,
) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(10)
        if discover:
            found = discover()
            if found and found not in bound and found not in pending:
                config.log(f"tailnet address {found} appeared")
                pending.append(found)
        for host in list(pending):
            server = _bind(config, handler, host, port)
            if server is not None:
                bound[host] = server
                pending.remove(host)
        if not pending and not discover:
            return


def _pretty(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
