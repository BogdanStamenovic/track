"""Sonnet scouts: fan out to `claude -p` to research sellers and listings.

track never talks to a marketplace or seller directly -- it spins up
short-lived, keyless Claude Code sessions (model sonnet, via `claude -p`)
that do the actual web research and hand back strict JSON. This module owns
the subprocess boundary so it can be exercised in tests without ever
touching the network or the real `claude` binary.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from .errors import ScoutError

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_WORKERS = 4

# `claude -p` auto-grants full tool access when there's no TTY to prompt --
# including raw Bash. Without this, a scout that hits a 403 will happily
# start spoofing headers and piping requests through third-party proxies to
# get around it (observed live against eBay) instead of just reporting what
# it found. Scouts get read-only web tools and nothing else.
SCOUT_ALLOWED_TOOLS = "WebSearch,WebFetch"

# Hard financial/wall-clock backstop. This isn't an interactive session
# Bogdan is watching -- it's an unattended job on a timer, so unlike normal
# usage it needs a real ceiling: a scout that starts refining queries in
# search of an exact price match (observed live) will happily burn its full
# timeout chasing one listing instead of returning what it already has.
SCOUT_MAX_BUDGET_USD = "0.50"


@dataclass(frozen=True, slots=True)
class ScoutFinding:
    source: str
    title: str
    price: float | None
    currency: str | None
    url: str | None


class Runner(Protocol):
    def __call__(
        self, cmd: list[str], *, input: str | None, timeout: int
    ) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    cmd: list[str], *, input: str | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, input=input, capture_output=True, text=True, timeout=timeout, check=False
    )


SOURCE_DISCOVERY_TEMPLATE = """You are a research scout. Assignment: {assignment}

Find who realistically sells this kind of item cheap, and where (marketplaces,
retailers, classifieds, forums -- whatever actually applies). Do not buy or
contact anyone. Respond with ONLY a JSON array, no prose, no markdown fences:

[{{"source": "<site or seller name>", "url": "<homepage or search url>", "notes": "<why it's a good/cheap source, 1 sentence>"}}]

Return at most 8 sources, ranked best first."""

LISTING_SCOUT_TEMPLATE = """You are a bargain-hunting scout. Assignment: {assignment}

Search {source_hint} right now for actual current listings that match. Do not
buy or contact anyone. You have WebSearch and WebFetch only -- no shell, no
proxies, no working around blocks. If a page 403s, is paywalled, or WebFetch
otherwise can't load it, just use the title/price/snippet from the search
result instead of trying another way in.

Budget: at most 3 tool calls total, then stop and answer with whatever you
have. Do not refine your query chasing an exact price for one specific
listing -- an approximate price from a search snippet is fine, and price:
null for a listing is fine too if no page will give it up. A handful of
good-enough results beats one perfect one. Respond with ONLY a JSON array,
no prose, no markdown fences:

[{{"source": "<site or seller name>", "title": "<listing title>", "price": <number or null>, "currency": "<ISO code or null>", "url": "<listing url>"}}]

Only include listings you can see are genuinely live right now. Return at most
10, cheapest/most underpriced first."""


def _run_claude(prompt: str, *, model: str, timeout: int, runner: Runner) -> str:
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--allowedTools",
        SCOUT_ALLOWED_TOOLS,
        "--max-budget-usd",
        SCOUT_MAX_BUDGET_USD,
    ]
    try:
        result = runner(cmd, input=prompt, timeout=timeout)
    except FileNotFoundError as exc:
        raise ScoutError("claude CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScoutError(f"scout timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise ScoutError(f"claude -p exited {result.returncode}: {result.stderr.strip()[:500]}")
    return result.stdout


def _parse_json_array(raw: str) -> list[dict]:
    text = raw.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ScoutError(f"scout output had no JSON array: {text[:200]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ScoutError(f"scout output was not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ScoutError("scout output JSON was not a list")
    return data


def discover_sources(
    assignment_text: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
) -> list[dict]:
    prompt = SOURCE_DISCOVERY_TEMPLATE.format(assignment=assignment_text)
    raw = _run_claude(prompt, model=model, timeout=timeout, runner=runner)
    return _parse_json_array(raw)


def scout_listings(
    assignment_text: str,
    source_hint: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
) -> list[ScoutFinding]:
    prompt = LISTING_SCOUT_TEMPLATE.format(assignment=assignment_text, source_hint=source_hint)
    raw = _run_claude(prompt, model=model, timeout=timeout, runner=runner)
    items = _parse_json_array(raw)
    findings = []
    for item in items:
        try:
            findings.append(
                ScoutFinding(
                    source=str(item["source"]),
                    title=str(item["title"]),
                    price=float(item["price"]) if item.get("price") is not None else None,
                    currency=item.get("currency"),
                    url=item.get("url"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # a malformed row shouldn't sink the whole scout
    return findings


def run_scouts(
    assignment_text: str,
    source_hints: list[str],
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
    max_workers: int = DEFAULT_MAX_WORKERS,
    warn: Callable[[str], None] = lambda _msg: None,
) -> list[ScoutFinding]:
    """Run one listing scout per source, in parallel, and pool the results.

    A single source's scout failing is logged via `warn` and skipped rather
    than failing the whole run -- the other sources may still turn up finds.
    """
    if not source_hints:
        return []
    findings: list[ScoutFinding] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(source_hints))) as pool:
        futures = {
            pool.submit(
                scout_listings, assignment_text, hint, model=model, timeout=timeout, runner=runner
            ): hint
            for hint in source_hints
        }
        for future, hint in futures.items():
            try:
                findings.extend(future.result())
            except ScoutError as exc:
                warn(f"scout for {hint!r} failed: {exc}")
    return findings
