"""Sonnet scouts: fan out to `claude -p` to research sellers and listings.

track never talks to a marketplace or a seller directly -- it spins up
short-lived, keyless Claude Code sessions (model sonnet, via `claude -p`)
that do the web research and hand back strict JSON. This module owns the
subprocess boundary so the rest of track can be exercised in tests without
touching the network or the real `claude` binary.

Three questions get asked this way, all of them about one assignment: who
sells this cheap, what is on offer right now, and -- once, when the
assignment is created -- what time of day is worth checking it. The last one
shares this module because it shares the whole containment apparatus below,
not because scheduling is market research.

Containment is the point of this module, so it is spelled out here.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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

# The runaway ceiling. This is not an interactive session anybody is watching,
# it is a job on a timer that re-arms itself, so it needs a hard stop: a scout
# that starts refining queries in search of an exact price match (observed
# live) will happily burn its whole timeout chasing one listing.
#
# What it protects depends on how `claude` is authenticated. Against an API
# key it caps billable spend; against a subscription it caps how much of the
# plan's usage allowance one scout can eat before the rest of the run, and the
# other assignments on the same timer, go hungry. The units are dollars
# because that is the flag the CLI offers, not because a bill is implied.
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
    # Provenance. All optional: a scout that cannot see one of these says so
    # with a null rather than filling it in, and a listing is still worth
    # recording without them.
    rationale: str | None = None  # the scout's own reason, not a template
    condition: str | None = None  # "used", "refurbished", "Grade B", ...
    posted_at: str | None = None  # ISO date the seller posted it
    age_days: float | None = None  # relative age, when there is no date
    product_year: int | None = None  # the model's release year, not the ad's


@dataclass(frozen=True, slots=True)
class ListingCheck:
    """What a re-check found at one listing's URL.

    `state` is deliberately four-valued rather than a boolean, because the
    difference between "the page says this sold" and "the site would not talk
    to us" is the whole difference between retiring a listing and libelling
    one.
    """

    url: str
    state: str  # "live" | "gone" | "blocked" | "unknown"
    price: float | None = None
    note: str | None = None


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


MARKET_CLAUSE = """
MARKET: {market}. This is a hard constraint, not a preference. Only sources
that actually serve a buyer located there count -- local marketplaces and
classifieds, local retailers, and sellers who genuinely ship there at a sane
cost. A large foreign marketplace the buyer cannot practically order from is
not a source, however cheap its listings look. Prefer the local-language
sites real buyers there use over the international ones. Quote prices in the
currency the listing is actually in; do not convert.
"""

SOURCE_DISCOVERY_TEMPLATE = """You are a market research scout. Assignment: {assignment}
{market_clause}
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
{market_clause}
Search {source_hint} right now for current listings that match.
Do not buy anything and do not contact anyone.

{block_policy}

Budget: at most 3 tool calls total, then stop and answer with whatever you
have. Do not refine your query chasing an exact price for one listing -- an
approximate price from a search snippet is fine, and `null` is fine when no
page will give one up. A handful of good-enough results beats one perfect
one. Respond with ONLY a JSON array, no prose, no markdown fences:

[{{"source": "<site or seller name>", "title": "<listing title>", "price": <number or null>, "currency": "<ISO code or null>", "url": "<listing url>", "why": "<why this one is worth a look, one specific sentence>", "condition": "<new|used|refurbished|for parts|the grade the listing states|null>", "posted": "<YYYY-MM-DD the seller posted it, or null>", "age_days": <days since it was posted, or null>, "model_year": <the year this MODEL was released, not the year of the ad, or null>}}]

Every field after `url` is optional and **`null` is the correct answer when
the page does not say** -- do not spend a tool call establishing one and do
not guess. `posted` and `age_days` are two spellings of the same fact:
give whichever the site states and null for the other. `why` is the
exception worth a sentence of thought: say what is actually notable about
*this* listing -- the specification, the condition, how its price compares
to the others you just saw -- not a restatement of the assignment. It is
shown to a human deciding whether to open the link.

Only listings you can actually see are live. At most 10, cheapest first."""


# Provenance fields are advisory, so every one of these returns None rather
# than raising: a scout that writes "unknown" into a date must not sink the
# listing it was describing.

_NON_ANSWERS = frozenset({"", "null", "none", "unknown", "n/a", "na", "-", "?"})


def _text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if cleaned.lower() in _NON_ANSWERS:
        return None
    return cleaned[:limit] or None


def _iso_date(value: Any) -> str | None:
    """A YYYY-MM-DD the scout actually read off the page, or nothing.

    Deliberately strict. A scout handed a relative age ("3 days ago") should
    put it in `age_days`; accepting a loose date here would turn its guess at
    the arithmetic into a stored fact.
    """
    text = _text(value, limit=10)
    if text is None:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 40_000.0 else None


def _year(value: Any) -> int | None:
    """A plausible model year. 1990..next year, so a hallucinated 2050 or a
    scout that answered with a price is dropped rather than stored."""
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1990 <= year <= datetime.now(timezone.utc).year + 1 else None


# Re-checking fetches a page per listing, where a listing scout runs a couple
# of searches, so it needs longer than DEFAULT_TIMEOUT. Six URLs measured at
# 20s; this is nine times that, so that one slow site cannot cost the batch.
CHECK_TIMEOUT = 180

CHECK_SCOUT_TEMPLATE = """You are checking whether listings are still for sale.
Fetch each URL below and report what you find. Do not buy anything and do not
contact anyone.

{block_policy}

For each URL answer with exactly one `state`:
  "live"    - the listing page loaded and the item is still on offer
  "gone"    - the page loaded and says the item is sold, reserved, expired,
              removed, or the URL returns 404 / "no such advert"
  "blocked" - the SITE refused you: 403, an anti-bot wall, a login gate, a
              captcha, a paywall. This says nothing about the listing.
  "unknown" - anything else: a timeout, a network error, or a page you could
              reach but could not read an answer from

`blocked` and `unknown` are correct, useful answers. Do not guess "gone" from
a failed fetch and do not guess "live" from a search result -- only from the
listing page itself. Give `price` when the page shows one, else null.

URLs:
{urls}

Budget: at most {budget} tool calls, then answer with what you have; anything
you did not reach is "unknown". Respond with ONLY a JSON array, no prose, no
markdown fences:

[{{"url": "<the url, copied exactly>", "state": "<live|gone|blocked|unknown>", "price": <number or null>, "note": "<what the page actually said, one short phrase>"}}]"""


def check_listings(
    urls: list[str],
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = CHECK_TIMEOUT,
    runner: Runner = _default_runner,
    max_budget_usd: str = SCOUT_MAX_BUDGET_USD,
) -> tuple[list[ListingCheck], float]:
    """Re-check a batch of listing URLs in one scout.

    One scout for the whole batch rather than one per listing: this runs after
    every research cycle, on top of the scouts that cycle already spent, and a
    per-listing scout would multiply the cost of a run by the size of the
    back catalogue.
    """
    if not urls:
        return [], 0.0
    prompt = CHECK_SCOUT_TEMPLATE.format(
        block_policy=BLOCK_POLICY,
        urls="\n".join(urls),
        budget=len(urls) + 2,
    )
    raw, cost = _run_claude(
        prompt, model=model, timeout=timeout, runner=runner, max_budget_usd=max_budget_usd
    )
    wanted = set(urls)
    checks: list[ListingCheck] = []
    for item in _parse_json_array(raw):
        url = _text(item.get("url"), limit=2000)
        if url not in wanted:
            continue  # a scout that invented a URL does not get to retire one
        state = (_text(item.get("state"), limit=20) or "").lower()
        if state not in {"live", "gone", "blocked", "unknown"}:
            state = "unknown"
        try:
            price = float(item["price"]) if item.get("price") is not None else None
        except (TypeError, ValueError):
            price = None
        checks.append(
            ListingCheck(url=url, state=state, price=price, note=_text(item.get("note"), limit=200))
        )
    return checks, cost


@dataclass(frozen=True, slots=True)
class ScheduleAdvice:
    """When to check an assignment, and the advisor's reason for saying so."""

    check_at: str  # "HH:MM", 24-hour, the operator's local time
    rationale: str
    cost_usd: float = 0.0


# One tool call, not three: this is a judgement about a category's rhythm,
# not a price hunt, and the difference between a well-reasoned 07:00 and a
# well-reasoned 08:00 is not worth a web search. Short timeout for the same
# reason -- an advisor that stalls must not hold up `track add`.
ADVICE_TIMEOUT = 60

SCHEDULE_ADVICE_TEMPLATE = """You are advising on when, in the day, to check for new
listings matching this assignment: {assignment}
{market_clause}
Pick ONE daily wall-clock time, 24-hour, in the BUYER's local time.

Reason about this category's actual rhythm, not a generic answer:
- when do sellers in this category post -- evenings, weekday mornings,
  weekends? A check shortly AFTER the posting peak sees the most new stock.
- how fast does a good listing get taken? Something that sells within the
  hour is worth catching early in the day; something that sits for a week is
  not, and an unsociable hour buys nothing.
- when could the buyer actually act on what you found? A find at 03:00 that
  is gone by 09:00 was never really a find.

Prefer a time between 06:00 and 23:00 unless the category genuinely rewards
an unsociable one, and say so if you pick one.

{block_policy}

Budget: at most 1 tool call, and none is fine -- you are being asked for
judgement, not research. Respond with ONLY a JSON object, no prose, no
markdown fences:

{{"check_at": "<HH:MM>", "why": "<two sentences at most: the rhythm you are
fitting, and what checking at that hour buys over checking at another>"}}"""


def _parse_json_object(raw: str) -> dict[str, Any]:
    """The richest JSON object in a scout's answer.

    Same discipline as `_parse_json_array` and for the same reason: an answer
    with a stray `{` in its prose, or an empty object before the real one,
    must not cost the whole call. Richest wins, so `{}` never beats a real
    answer.
    """
    text = raw.strip()
    decoder = json.JSONDecoder()
    best: dict[str, Any] | None = None
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            value, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (best is None or len(value) > len(best)):
            best = value
    if best is None:
        raise ScoutError(f"advisor output had no JSON object: {text[:200]!r}")
    return best


_CLOCK_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def recommend_check_time(
    assignment_text: str,
    *,
    market: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: int = ADVICE_TIMEOUT,
    runner: Runner = _default_runner,
    max_budget_usd: str = SCOUT_MAX_BUDGET_USD,
) -> ScheduleAdvice:
    """Ask one Sonnet session what time of day to check this assignment.

    Raises ScoutError on anything the caller should not act on -- no claude
    binary, a timeout, an answer with no clock time in it. The caller is
    expected to fall back to a plain interval rather than propagate it: a
    missing opinion about the best hour must never stop an assignment being
    tracked at all.
    """
    prompt = SCHEDULE_ADVICE_TEMPLATE.format(
        assignment=assignment_text,
        block_policy=BLOCK_POLICY,
        market_clause=_market_clause(market),
    )
    raw, cost = _run_claude(
        prompt, model=model, timeout=timeout, runner=runner, max_budget_usd=max_budget_usd
    )
    answer = _parse_json_object(raw)
    match = _CLOCK_RE.match(_text(answer.get("check_at"), limit=5) or "")
    if not match or int(match.group(1)) > 23:
        raise ScoutError(f"advisor gave no usable time: {str(answer.get('check_at'))[:60]!r}")
    rationale = _text(answer.get("why"), limit=500)
    if not rationale:
        raise ScoutError("advisor gave a time with no reason for it")
    return ScheduleAdvice(
        check_at=f"{int(match.group(1)):02d}:{match.group(2)}",
        rationale=rationale,
        cost_usd=cost,
    )


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
    """The listing array out of a scout's answer, whatever else it said.

    Taking the span from the first `[` to the last `]` looks right and is
    wrong the moment a scout writes two arrays, because the span then covers
    both and the text between them. That is not hypothetical: two runs lost a
    whole source each to `Extra data: line 2 column 1 (char 3)`, which is
    exactly what `[]` followed by the real array produces. A trailing "see
    [1] for details" does the same thing.

    So every `[` is tried as the start of a value and the richest array wins.
    Richest rather than first, because the empty one usually comes first; and
    an array of listings always beats a footnote marker.
    """
    text = raw.strip()
    decoder = json.JSONDecoder()
    best: list[dict[str, Any]] | None = None
    for start in (i for i, ch in enumerate(text) if ch == "["):
        try:
            value, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, list):
            continue
        items = [item for item in value if isinstance(item, dict)]
        if best is None or len(items) > len(best):
            best = items
    if best is None:
        raise ScoutError(f"scout output had no JSON array: {text[:200]!r}")
    return best


def _market_clause(market: str | None) -> str:
    return MARKET_CLAUSE.format(market=market) if market else ""


def discover_sources(
    assignment_text: str,
    *,
    market: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
    max_budget_usd: str = SCOUT_MAX_BUDGET_USD,
) -> tuple[list[dict[str, Any]], float]:
    prompt = SOURCE_DISCOVERY_TEMPLATE.format(
        assignment=assignment_text,
        block_policy=BLOCK_POLICY,
        market_clause=_market_clause(market),
    )
    raw, cost = _run_claude(
        prompt, model=model, timeout=timeout, runner=runner, max_budget_usd=max_budget_usd
    )
    return _parse_json_array(raw), cost


def scout_listings(
    assignment_text: str,
    source_hint: str,
    *,
    market: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = _default_runner,
    max_budget_usd: str = SCOUT_MAX_BUDGET_USD,
) -> ScoutResult:
    prompt = LISTING_SCOUT_TEMPLATE.format(
        assignment=assignment_text,
        source_hint=source_hint,
        block_policy=BLOCK_POLICY,
        market_clause=_market_clause(market),
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
                rationale=_text(item.get("why"), limit=400),
                condition=_text(item.get("condition"), limit=60),
                posted_at=_iso_date(item.get("posted")),
                age_days=_positive_number(item.get("age_days")),
                product_year=_year(item.get("model_year")),
            )
        )
    return ScoutResult(findings=findings, cost_usd=cost, blocked=blocked)


def run_scouts(
    assignment_text: str,
    source_hints: list[str],
    *,
    market: str | None = None,
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
                market=market,
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
