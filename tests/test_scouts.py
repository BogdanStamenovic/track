from __future__ import annotations

import subprocess

import pytest

from track.errors import ScoutError
from track.scouts import discover_sources, run_scouts, scout_listings


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_discover_sources_parses_json_array() -> None:
    raw = '[{"source": "eBay", "url": "https://ebay.com", "notes": "cheap"}]'

    def runner(cmd, *, input, timeout):
        return _completed(raw)

    result = discover_sources("a laptop", runner=runner)
    assert result == [{"source": "eBay", "url": "https://ebay.com", "notes": "cheap"}]


def test_scout_invocation_restricts_tools_to_web_only() -> None:
    seen_cmd = {}

    def runner(cmd, *, input, timeout):
        seen_cmd["cmd"] = cmd
        return _completed("[]")

    discover_sources("a laptop", runner=runner)
    cmd = seen_cmd["cmd"]
    assert "--allowedTools" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash" not in allowed
    assert "WebSearch" in allowed
    assert "WebFetch" in allowed


def test_scout_invocation_caps_spend() -> None:
    seen_cmd = {}

    def runner(cmd, *, input, timeout):
        seen_cmd["cmd"] = cmd
        return _completed("[]")

    discover_sources("a laptop", runner=runner)
    cmd = seen_cmd["cmd"]
    assert "--max-budget-usd" in cmd


def test_discover_sources_tolerates_prose_around_json() -> None:
    raw = 'Sure, here you go:\n[{"source": "eBay", "url": null, "notes": "x"}]\nHope that helps!'

    def runner(cmd, *, input, timeout):
        return _completed(raw)

    result = discover_sources("a laptop", runner=runner)
    assert result[0]["source"] == "eBay"


def test_scout_output_not_json_raises() -> None:
    def runner(cmd, *, input, timeout):
        return _completed("not json at all")

    with pytest.raises(ScoutError):
        discover_sources("a laptop", runner=runner)


def test_nonzero_exit_raises() -> None:
    def runner(cmd, *, input, timeout):
        return _completed("", returncode=1, stderr="boom")

    with pytest.raises(ScoutError, match="boom"):
        discover_sources("a laptop", runner=runner)


def test_claude_not_found_raises_scout_error() -> None:
    def runner(cmd, *, input, timeout):
        raise FileNotFoundError()

    with pytest.raises(ScoutError, match="not found"):
        discover_sources("a laptop", runner=runner)


def test_scout_listings_skips_malformed_rows() -> None:
    raw = (
        '[{"source": "eBay", "title": "ThinkPad", "price": 300, "currency": "USD", '
        '"url": "https://x"}, {"source": "eBay"}]'
    )

    def runner(cmd, *, input, timeout):
        return _completed(raw)

    findings = scout_listings("a laptop", "eBay", runner=runner)
    assert len(findings) == 1
    assert findings[0].price == 300.0


def test_scout_listings_price_null_is_none() -> None:
    raw = '[{"source": "eBay", "title": "x", "price": null, "currency": null, "url": null}]'

    def runner(cmd, *, input, timeout):
        return _completed(raw)

    findings = scout_listings("a laptop", "eBay", runner=runner)
    assert findings[0].price is None


def test_run_scouts_pools_results_across_sources() -> None:
    def runner(cmd, *, input, timeout):
        return _completed('[{"source": "s", "title": "t", "price": 1, "currency": "USD", "url": null}]')

    findings = run_scouts("a laptop", ["eBay", "Craigslist"], runner=runner)
    assert len(findings) == 2


def test_run_scouts_skips_failing_source_and_warns() -> None:
    def runner(cmd, *, input, timeout):
        if "eBay" in input:
            return _completed("", returncode=1, stderr="down")
        return _completed('[{"source": "s", "title": "t", "price": 1, "currency": "USD", "url": null}]')

    warnings = []
    findings = run_scouts(
        "a laptop", ["eBay", "Craigslist"], runner=runner, warn=warnings.append
    )
    assert len(findings) == 1
    assert any("eBay" in w for w in warnings)


def test_run_scouts_empty_sources_returns_empty() -> None:
    assert run_scouts("a laptop", [], runner=lambda *a, **k: _completed("[]")) == []
