"""Everything after `track web`.

`track.cli` hands this module the argv remainder, the database path the store
resolved, and its own logger, then stays out of the way -- so adding a flag here
never means editing `track/cli.py`, and one owner per file keeps the two halves
of this repo out of each other's merge conflicts.

Keeps stdout clean; all progress, warnings, and errors go to stderr. The one
command that produces real output on stdout is `info`.

Exit codes: 0 success, 1 one or more operations failed, 2 usage error,
130 interrupt.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

from .. import __version__
from .data import DEFAULT_DB_PATH, Summary, WebError, connect, read_schema, summarize
from .server import Config, gaps_for, serve, tailnet_address

DEFAULT_PORT = 8791

#: Read when the corresponding flag is absent. They exist so the systemd unit can
#: be configured through an EnvironmentFile instead of a rewritten ExecStart --
#: `track web serve` with no arguments has to be the whole command line, or the
#: installer would be editing a unit file every time a port changes.
PORT_ENV = "TRACK_WEB_PORT"
HOSTS_ENV = "TRACK_WEB_HOSTS"


def _env_port() -> int | None:
    raw = os.environ.get(PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_hosts() -> list[str]:
    raw = os.environ.get(HOSTS_ENV, "")
    return [h for h in raw.replace(",", " ").split() if h]


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="track web",
        description="A local read-only web viewer over track's findings database",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database path (default: the one track uses)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print detailed progress")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    # Repeated on every subcommand with SUPPRESS defaults, so `track-web serve -v`
    # works as well as `track-web -v serve` without the subparser's default
    # clobbering a value already set on the top-level parser.
    common = _ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)

    subs = parser.add_subparsers(dest="command")

    srv = subs.add_parser("serve", help="run the viewer (default command)", parents=[common])
    srv.add_argument(
        "--host",
        action="append",
        metavar="ADDR",
        help=f"address to bind; repeatable. Default: ${HOSTS_ENV} if set, else "
        "127.0.0.1 plus this host's tailnet address if it has one. Never binds a "
        "wildcard implicitly.",
    )
    srv.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,
        help=f"port (default: ${PORT_ENV}, else {DEFAULT_PORT})",
    )

    inf = subs.add_parser(
        "info",
        help="print what the database holds and which fields are missing",
        parents=[common],
    )
    inf.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _collect(db_path: Path) -> Summary:
    conn = connect(db_path)
    try:
        return summarize(conn, db_path, gaps_for(read_schema(conn)))
    finally:
        conn.close()


def _print_info(data: Summary) -> None:
    out = sys.stdout
    print(f"database   {data.db}", file=out)
    print(f"sightings  {data.sightings}  ->  {data.listings} distinct listings", file=out)
    print(file=out)
    print("assignments:", file=out)
    for a in data.assignments:
        dead = f"  {a.dead_count} gone" if a.dead_count else ""
        print(
            f"  {a.id}  {a.listing_count:>4} listings  {a.runs_count:>3} runs  "
            f"{a.status:<8}{dead} {a.text[:56]}",
            file=out,
        )
    print(file=out)
    print("fields resolved on findings:", file=out)
    for logical, column in sorted(data.resolved.items()):
        print(f"  {logical:<17} {column or '-- absent --'}", file=out)
    if data.gaps:
        print(file=out)
        print("not recorded anywhere, so the page will say so:", file=out)
        for gap in data.gaps:
            print(f"  - {gap}", file=out)
    print(file=out)
    print(
        f"blank values: {data.without_price} without price, "
        f"{data.without_score} without score, {data.without_url} without link, "
        f"{data.without_reason} without a reason",
        file=out,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    db_path: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """`db_path` and `log` come from `track.cli`; both are optional so that
    `python -m track.web` still works on its own."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"track web: error: {exc}", file=sys.stderr)
        return 2

    if args.db is None:
        args.db = db_path or DEFAULT_DB_PATH

    host_log = log

    def say(message: str) -> None:
        if args.quiet:
            return
        if host_log is not None:
            host_log(message)
        else:
            print(message, file=sys.stderr)

    def vlog(message: str) -> None:
        if args.verbose:
            say(message)

    command = args.command or "serve"

    try:
        if command == "info":
            data = _collect(args.db)
            if args.json:
                print(json.dumps(data.as_dict(), indent=2, default=str))
            else:
                _print_info(data)
            return 0

        explicit = list(getattr(args, "host", None) or []) or _env_hosts()
        hosts = list(explicit)
        discover = None
        if not hosts:
            hosts = ["127.0.0.1"]
            tailnet = tailnet_address()
            if tailnet:
                hosts.append(tailnet)
                vlog(f"found tailnet address {tailnet}")
            else:
                say("no tailnet address yet; serving localhost and watching for one")
            # Only when the address was auto-detected: an explicit --host list is
            # exactly what he asked for and must not grow behind his back.
            discover = tailnet_address

        # Fail here rather than after binding, so a bad path is a clean error.
        connect(args.db).close()

        port = getattr(args, "port", None) or _env_port() or DEFAULT_PORT
        servers = serve(Config(db_path=args.db, log=say), hosts, port, discover=discover)
        say(f"track web {__version__} serving {args.db}")
        for server in servers:
            host, bound = server.server_address[:2]
            say(f"  http://{host!s}:{bound}/")

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
        say("shutting down")
        for server in servers:
            server.shutdown()
        return 0
    except WebError as exc:
        print(f"track web: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
