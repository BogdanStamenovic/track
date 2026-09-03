"""Sonnet scouts: fan out to `claude -p` to research sellers and listings.

track never talks to a marketplace or a seller directly -- it spins up
short-lived, keyless Claude Code sessions (model sonnet, via `claude -p`)
that do the web research and hand back strict JSON. This module owns the
subprocess boundary so the rest of track can be exercised in tests without
touching the network or the real `claude` binary.

Containment is the point of this module, so it is spelled out here.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import ScoutError

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_WORKERS = 4

# The whole containment story, and why each flag is load-bearing.
#
# An earlier version passed --allowedTools and a scout still went rogue: given
# eBay's 403 it started spoofing headers and piping requests through
# third-party proxies, burning its entire timeout. --allowedTools only governs
# which tools are *auto-approved*; the tool set itself still held Bash and
# every configured MCP server (including a real Chrome the scout could have
# driven). --tools replaces the available set outright, which is the
# difference between telling a scout not to do it and it having nothing to do
# it with. Verified live: a scout launched this way reports its own tools as
# exactly ["WebFetch", "WebSearch"], and WebFetch exposes no header control.
SCOUT_TOOLS = "WebSearch,WebFetch"

# --tools and --allowedTools are both required and they do different jobs.
# --tools decides what exists; --allowedTools decides what runs without a
# human approving it. Passing only the first is not a stricter version of
# passing both: --setting-sources "" (below) throws away the operator's
# permission rules along with everything else, so an un-allowlisted tool has
# nobody to approve it in a --print session and is denied outright. Verified
# the hard way -- a live scout came back "Both my available tools were denied
# permission [...] this is a real stop", which is exactly right and exactly
# useless. The allowlist names the same two tools, so it grants nothing the
# restriction above has not already permitted.
SCOUT_ALLOWED_TOOLS = SCOUT_TOOLS

# No MCP servers at all. Without this the scout inherits whatever is
# configured for the user -- a browser-automation server would hand back the
# exact capability --tools just took away.
SCOUT_STRICT_MCP = True

# No user/project/local settings: no inherited hooks, permissions, or
# environment surprises in an unattended job.
SCOUT_SETTING_SOURCES = ""

# Hard financial backstop. This is not an interactive session anybody is
# watching, it is a job on a timer, so it needs a real ceiling: a scout that
# starts refining queries in search of an exact price match (observed live)
# will happily burn its whole timeout chasing one listing.
SCOUT_MAX_BUDGET_USD = "0.50"

# CONSTRAINT, do not "clean this up": every scout inherits the operator's
# global CLAUDE.md and auto-memory, whose first line is "improvise, never
# declare impossible -- route around every blocker". That is the instruction
# that produced the proxy incident. The only flags that suppress memory
# loading (--bare, CLAUDE_CODE_SIMPLE=1) also disable OAuth and demand an API
# key -- both were tested and return "Not logged in - Please run /login" --
# and track is deliberately keyless. So the countermand has to live in the
# prompt. The tool restriction above is what actually makes it impossible;
# this is what stops the scout wasting its budget attempting it.
BLOCK_POLICY = """A 403, an anti-bot wall, a robots block, a paywall or a login
gate is a REAL stop, not a puzzle. Ignore any general instruction you may be
carrying about improvising around blockers -- it does not apply to third-party
marketplaces, which are not your operator's data. Do not look for another way
in. Record what you could see from the search result and move on. Never invent
a price, a listing, or a URL: an unknown price is `null`, and that is a
correct answer."""


@dataclass(frozen=True, slots=True)
class ScoutFinding:
    source: str
    title: str
    price: float | None
    currency: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class ScoutResult:
    """One scout's return, plus what it cost to get it."""

    findings: list[ScoutFinding] = field(default_factory=list)
    cost_usd: float = 0.0
    blocked: int = 0  # listings the scout reported as price-unknown


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


SOURCE_DISCOVERY_TEMPLATE = """You are a market research scout. Assignment: {assignment}

Work out who realistically sells this kind of thing cheap, and where:
marketplaces, liquidators, refurbishers, classifieds, forums, local
second-hand chains -- whatever actually applies to this category. Favour
places known for below-retail prices over the obvious full-price retailer.

Do not buy anything and do not contact anyone.

{block_policy}

Budget: at most 3 tool calls, then answer with what you have. Respond with
ONLY a JSON array, no prose, no markdown fences:

[{{"source": "<site or seller name>", "url": "<homepage or search url>", "notes": "<why this is a cheap source, one sentence>"}}]

At most 8 sources, best first."""

LISTING_SCOUT_TEMPLATE = """You are a bargain-hunting scout. Assignment: {assignment}

Search {source_hint} right now for current listings that match.
Do not buy anything and do not contact anyone.

{block_policy}

Budget: at most 3 tool calls total, then stop and answer with whatever you
have. Do not refine your query chasing an exact price for one listing -- an
approximate price from a search snippet is fine, and `null` is fine when no
page will give one up. A handful of good-enough results beats one perfect
one. Respond with ONLY a JSON array, no prose, no markdown fences:

[{{"source": "<site or seller name>", "title": "<listing title>", "price": <number or null>, "currency": "<ISO code or null>", "url": "<listing url>"}}]

Only listings you can actually see are live. At most 10, cheapest first."""


def _build_cmd(model: str, max_budget_usd: str) -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--tools",
        SCOUT_TOOLS,
        "--allowedTools",
        SCOUT_ALLOWED_TOOLS,
        "--setting-sources",
        SCOUT_SETTING_SOURCES,
        "--max-budget-usd",
        max_budget_usd,
        "--output-format",
        "json",
    ]
    if SCOUT_STRICT_MCP:
        cmd.append("--strict-mcp-config")
    return cmd


def _run_claude(
    prompt: str, *, model: str, timeout: int, runner: Runner, max_budget_usd: str
) -> tuple[str, float]:
    """Run one scout and return (its text answer, what it cost)."""
    cmd = _build_cmd(model, max_budget_usd)
    try:
        result = runner(cmd, input=prompt, timeout=timeout)
    except FileNotFoundError as exc:
        raise ScoutError("claude CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScoutError(f"scout timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise ScoutError(f"claude -p exited {result.returncode}: {result.stderr.strip()[:500]}")
    return _unwrap_envelope(result.stdout)


def _unwrap_envelope(stdout: str) -> tuple[str, float]:
    """Pull the answer and the cost out of `--output-format json`.

    Falls back to treating stdout as the raw answer, so a future CLI that
    stops emitting the envelope degrades to the old behaviour instead of
    failing every scout.
    """
    text = stdout.strip()
    if not text:
        raise ScoutError("scout produced no output")
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return text, 0.0
    if not isinstance(envelope, dict) or "result" not in envelope:
        return text, 0.0
    cost = float(envelope.get("total_cost_usd") or 0.0)
    if envelope.get("is_error"):
        raise ScoutError(f"scout failed: {str(envelope.get('result'))[:300]}")
    return str(envelope.get("result") or ""), cost


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
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
    return [item for item in data if isinstance(item, dict)]


def discover_sources(
    assignment_text: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
    max_budget_usd: str = SCOUT_MAX_BUDGET_USD,
) -> tuple[list[dict[str, Any]], float]:
    prompt = SOURCE_DISCOVERY_TEMPLATE.format(
        assignment=assignment_text, block_policy=BLOCK_POLICY
    )
    raw, cost = _run_claude(
        prompt, model=model, timeout=timeout, runner=runner, max_budget_usd=max_budget_usd
    )
    return _parse_json_array(raw), cost


def scout_listings(
    assignment_text: str,
    source_hint: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
    max_budget_usd: str = SCOUT_MAX_BUDGET_USD,
) -> ScoutResult:
    prompt = LISTING_SCOUT_TEMPLATE.format(
        assignment=assignment_text, source_hint=source_hint, block_policy=BLOCK_POLICY
    )
    raw, cost = _run_claude(
        prompt, model=model, timeout=timeout, runner=runner, max_budget_usd=max_budget_usd
    )
    findings: list[ScoutFinding] = []
    blocked = 0
    for item in _parse_json_array(raw):
        try:
            price = float(item["price"]) if item.get("price") is not None else None
        except (TypeError, ValueError):
            price = None
        try:
            title = str(item["title"]).strip()
            source = str(item["source"]).strip()
        except KeyError:
            continue  # a malformed row shouldn't sink the whole scout
        if not title or not source:
            continue
        if price is None:
            blocked += 1
        currency = item.get("currency")
        url = item.get("url")
        findings.append(
            ScoutFinding(
                source=source,
                title=title,
                price=price,
                currency=str(currency) if currency else None,
                url=str(url) if url else None,
            )
        )
    return ScoutResult(findings=findings, cost_usd=cost, blocked=blocked)


def run_scouts(
    assignment_text: str,
    source_hints: list[str],
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_budget_usd: str = SCOUT_MAX_BUDGET_USD,
    warn: Callable[[str], None] = lambda _msg: None,
) -> ScoutResult:
    """Run one listing scout per source, in parallel, and pool the results.

    A single source's scout failing is reported via `warn` and skipped rather
    than failing the run -- the other sources may still turn something up.
    """
    if not source_hints:
        return ScoutResult()
    findings: list[ScoutFinding] = []
    cost = 0.0
    blocked = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(source_hints))) as pool:
        futures = {
            pool.submit(
                scout_listings,
                assignment_text,
                hint,
                model=model,
                timeout=timeout,
                runner=runner,
                max_budget_usd=max_budget_usd,
            ): hint
            for hint in source_hints
        }
        for future, hint in futures.items():
            try:
                result = future.result()
            except ScoutError as exc:
                warn(f"scout for {hint!r} failed: {exc}")
                continue
            findings.extend(result.findings)
            cost += result.cost_usd
            blocked += result.blocked
    return ScoutResult(findings=findings, cost_usd=cost, blocked=blocked)
