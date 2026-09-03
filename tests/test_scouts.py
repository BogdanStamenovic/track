"""Scout tests.

The important ones here are about containment, not parsing: a scout that can
reach a shell can defeat a 403, and the whole point of this module is that it
cannot.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from conftest import completed, envelope

from track.errors import ScoutError
from track.scouts import (
    SCOUT_ALLOWED_TOOLS,
    SCOUT_TOOLS,
    ScoutFinding,
    discover_sources,
    run_scouts,
    scout_listings,
)

LISTINGS = (
    '[{"source": "eBay", "title": "ThinkPad P52", "price": 310.0, '
    '"currency": "USD", "url": "https://ebay.com/itm/1"}]'
)


def _capturing(stdout: str) -> tuple[dict[str, Any], Any]:
    seen: dict[str, Any] = {}

    def runner(cmd: list[str], *, input: str | None, timeout: int):
        seen["cmd"] = cmd
        seen["prompt"] = input
        return completed(stdout)

    return seen, runner


# -- containment ---------------------------------------------------------


def test_scout_replaces_the_tool_set_rather_than_the_allowlist() -> None:
    """--allowedTools only auto-approves; --tools is what removes Bash."""
    seen, runner = _capturing(envelope("[]"))
    discover_sources("a laptop", runner=runner)
    cmd = seen["cmd"]

    assert "--tools" in cmd, "must restrict the available set, not just the allowlist"
    tools = cmd[cmd.index("--tools") + 1]
    assert tools == SCOUT_TOOLS
    assert set(tools.split(",")) == {"WebSearch", "WebFetch"}
    assert "Bash" not in tools


def test_the_two_surviving_tools_are_also_pre_approved() -> None:
    """Regression, found live: --tools without --allowedTools denies everything.

    Dropping the operator's settings takes their permission rules with it, so
    in a --print session nobody is left to approve a tool call. A real scout
    came back "Both my available tools were denied permission" -- correct
    behaviour, zero findings.
    """
    seen, runner = _capturing(envelope("[]"))
    discover_sources("a laptop", runner=runner)
    cmd = seen["cmd"]

    assert "--allowedTools" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert set(allowed.split(",")) == set(SCOUT_TOOLS.split(","))
    assert allowed == SCOUT_ALLOWED_TOOLS
    assert "Bash" not in allowed, "the allowlist must not widen what --tools left"


def test_scout_gets_no_mcp_servers_and_no_inherited_settings() -> None:
    """An inherited browser-automation server would restore what --tools took."""
    seen, runner = _capturing(envelope("[]"))
    discover_sources("a laptop", runner=runner)
    cmd = seen["cmd"]

    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_scout_has_a_spend_ceiling_and_asks_for_sonnet() -> None:
    seen, runner = _capturing(envelope("[]"))
    discover_sources("a laptop", runner=runner)
    cmd = seen["cmd"]

    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert float(cmd[cmd.index("--max-budget-usd") + 1]) > 0


def test_prompt_countermands_the_inherited_improvise_directive() -> None:
    """Scouts inherit the operator's CLAUDE.md; the prompt has to override it."""
    seen, runner = _capturing(envelope("[]"))
    scout_listings("a laptop", "eBay", runner=runner)
    prompt = seen["prompt"]

    assert "REAL stop" in prompt
    assert "improvising around blockers" in prompt
    assert "Never invent" in prompt


# -- envelope handling ---------------------------------------------------


def test_cost_is_read_off_the_json_envelope() -> None:
    _seen, runner = _capturing(envelope(LISTINGS, cost=0.42))
    result = scout_listings("a laptop", "eBay", runner=runner)
    assert result.cost_usd == pytest.approx(0.42)


def test_envelope_error_becomes_a_scout_error() -> None:
    _seen, runner = _capturing(envelope("Not logged in", is_error=True))
    with pytest.raises(ScoutError, match="Not logged in"):
        scout_listings("a laptop", "eBay", runner=runner)


def test_bare_stdout_still_parses_if_the_envelope_ever_disappears() -> None:
    _seen, runner = _capturing(LISTINGS)
    result = scout_listings("a laptop", "eBay", runner=runner)
    assert len(result.findings) == 1
    assert result.cost_usd == 0.0


def test_empty_output_is_an_error_not_an_empty_result() -> None:
    _seen, runner = _capturing("")
    with pytest.raises(ScoutError, match="no output"):
        scout_listings("a laptop", "eBay", runner=runner)


# -- parsing -------------------------------------------------------------


def test_listings_parse_into_findings() -> None:
    _seen, runner = _capturing(envelope(LISTINGS))
    result = scout_listings("a laptop", "eBay", runner=runner)
    assert result.findings == [
        ScoutFinding("eBay", "ThinkPad P52", 310.0, "USD", "https://ebay.com/itm/1")
    ]
    assert result.blocked == 0


def test_a_blocked_price_is_null_not_a_guess() -> None:
    raw = '[{"source": "eBay", "title": "ThinkPad", "price": null, "url": "https://e/1"}]'
    _seen, runner = _capturing(envelope(raw))
    result = scout_listings("a laptop", "eBay", runner=runner)

    assert result.findings[0].price is None
    assert result.blocked == 1


def test_prose_around_the_array_is_tolerated() -> None:
    _seen, runner = _capturing(envelope(f"Here you go:\n```json\n{LISTINGS}\n```"))
    assert len(scout_listings("a laptop", "eBay", runner=runner).findings) == 1


def test_a_malformed_row_does_not_sink_the_scout() -> None:
    raw = '[{"nope": 1}, {"source": "eBay", "title": "ok", "price": "not a number"}]'
    _seen, runner = _capturing(envelope(raw))
    findings = scout_listings("a laptop", "eBay", runner=runner).findings

    assert len(findings) == 1
    assert findings[0].price is None


def test_output_without_an_array_is_an_error() -> None:
    _seen, runner = _capturing(envelope("I could not find anything."))
    with pytest.raises(ScoutError, match="no JSON array"):
        scout_listings("a laptop", "eBay", runner=runner)


def test_nonzero_exit_is_an_error() -> None:
    def runner(cmd, *, input, timeout):
        return completed("", returncode=1, stderr="boom")

    with pytest.raises(ScoutError, match="exited 1"):
        discover_sources("a laptop", runner=runner)


def test_timeout_is_an_error() -> None:
    def runner(cmd, *, input, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with pytest.raises(ScoutError, match="timed out"):
        discover_sources("a laptop", runner=runner, timeout=5)


def test_missing_claude_binary_is_an_error() -> None:
    def runner(cmd, *, input, timeout):
        raise FileNotFoundError

    with pytest.raises(ScoutError, match="not found on PATH"):
        discover_sources("a laptop", runner=runner)


# -- fan-out -------------------------------------------------------------


def test_run_scouts_pools_findings_and_costs() -> None:
    def runner(cmd, *, input, timeout):
        source = "eBay" if "eBay" in (input or "") else "Craigslist"
        raw = f'[{{"source": "{source}", "title": "t", "price": 10.0, "url": "https://x/{source}"}}]'
        return completed(envelope(raw, cost=0.1))

    result = run_scouts("a laptop", ["eBay", "Craigslist"], runner=runner)
    assert len(result.findings) == 2
    assert result.cost_usd == pytest.approx(0.2)


def test_one_failing_scout_does_not_fail_the_run() -> None:
    def runner(cmd, *, input, timeout):
        if "eBay" in (input or ""):
            return completed("", returncode=1, stderr="nope")
        return completed(envelope('[{"source": "CL", "title": "t", "price": 1.0}]'))

    warnings: list[str] = []
    result = run_scouts("a laptop", ["eBay", "CL"], runner=runner, warn=warnings.append)

    assert len(result.findings) == 1
    assert any("eBay" in w for w in warnings)


def test_no_sources_means_no_subprocess_at_all() -> None:
    def runner(cmd, *, input, timeout):
        raise AssertionError("should not have run a scout")

    assert run_scouts("a laptop", [], runner=runner).findings == []
