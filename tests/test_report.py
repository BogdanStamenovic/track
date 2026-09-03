from __future__ import annotations

import pytest

from track.errors import ReportError
from track.models import Assignment, Finding, SourceStat
from track.report import build_summary, post_summary


def _assignment(max_price: float | None = None, text: str = "a cheap laptop") -> Assignment:
    return Assignment(
        id="a1",
        text=text,
        interval_seconds=3600,
        status="active",
        max_price=max_price,
        created_at="2026-09-03T00:00:00+00:00",
    )


def _finding(
    title: str = "ThinkPad P52",
    price: float | None = 310.0,
    score: float | None = 0.9,
    is_new: bool = True,
    url: str | None = "https://ebay.com/itm/1",
    source: str = "eBay",
) -> Finding:
    return Finding(
        id=1,
        assignment_id="a1",
        run_id=1,
        source=source,
        title=title,
        price=price,
        currency="USD",
        url=url,
        dedup_key=title,
        score=score,
        is_new=is_new,
        found_at="2026-09-03T00:00:00+00:00",
    )


# -- summary content -----------------------------------------------------


def test_summary_leads_with_the_assignment_and_the_counts() -> None:
    summary = build_summary(_assignment(), [_finding()], sources_checked=5)

    assert "a cheap laptop" in summary
    assert "5 sources checked" in summary
    assert "1 listings seen" in summary
    assert "1 new" in summary


def test_a_new_listing_is_reported_with_price_source_and_score() -> None:
    summary = build_summary(_assignment(), [_finding()], 5)

    assert "ThinkPad P52" in summary
    assert "310.00 USD" in summary
    assert "@ eBay" in summary
    assert "score 0.90" in summary
    assert "https://ebay.com/itm/1" in summary


def test_a_quiet_run_says_so_rather_than_repeating_old_finds() -> None:
    summary = build_summary(_assignment(), [_finding(is_new=False)], 5)
    assert "Nothing new this run." in summary
    assert "ThinkPad" not in summary


def test_new_listings_are_ranked_by_score() -> None:
    findings = [
        _finding("cheap-ish", 200.0, 0.5),
        _finding("bargain", 100.0, 0.95),
        _finding("meh", 400.0, 0.1),
    ]
    body = build_summary(_assignment(), findings, 3)
    assert body.index("bargain") < body.index("cheap-ish") < body.index("meh")


def test_only_the_top_five_are_listed_and_the_rest_are_counted() -> None:
    findings = [_finding(f"item{i}", 100.0 + i, 0.5) for i in range(9)]
    summary = build_summary(_assignment(), findings, 3)

    assert summary.count("• ") == 5
    assert "and 4 more new listings" in summary


def test_an_unknown_price_is_labelled_not_guessed() -> None:
    """A blocked site yields price: null; the summary must not invent one."""
    summary = build_summary(_assignment(), [_finding(price=None, score=None)], 3)

    assert "price unknown" in summary
    assert "score" not in summary.split("• ")[1].split("\n")[0]


def test_listings_over_the_ceiling_are_counted_out_not_shown() -> None:
    findings = [_finding("too dear", 900.0, 0.2), _finding("in budget", 300.0, 0.8)]
    summary = build_summary(_assignment(max_price=500.0), findings, 3)

    assert "too dear" not in summary
    assert "in budget" in summary
    assert "1 over the" in summary


def test_no_ceiling_means_nothing_is_filtered() -> None:
    summary = build_summary(_assignment(), [_finding("pricey", 9999.0, 0.1)], 3)
    assert "pricey" in summary
    assert "over the" not in summary


def test_cheap_sources_are_reported_because_that_is_half_the_assignment() -> None:
    stats = [
        SourceStat("Craigslist", 4, 4, 50.0, 75.0, 0.9, "USD"),
        SourceStat("eBay", 10, 10, 300.0, 420.0, 0.6, "USD"),
    ]
    summary = build_summary(_assignment(), [_finding()], 3, stats=stats)

    assert "Cheapest sources so far:" in summary
    assert "Craigslist: from 50.00 USD" in summary
    assert "median 75.00 USD" in summary


def test_a_source_that_blocks_prices_is_named_as_unreadable_not_as_cheap() -> None:
    stats = [SourceStat("WalledGarden", 3, 0, None, None, None, None)]
    summary = build_summary(_assignment(), [_finding()], 3, stats=stats)

    assert "no price readable from: WalledGarden" in summary
    assert "Cheapest sources" not in summary


def test_spend_is_reported_when_there_was_any() -> None:
    assert "$0.240" in build_summary(_assignment(), [_finding()], 3, cost_usd=0.24)
    assert "scouts:" not in build_summary(_assignment(), [_finding()], 3, cost_usd=0.0)


# -- delivery ------------------------------------------------------------


def test_the_assignment_agent_wins(monkeypatch) -> None:
    """A scheduled run has no session, so the channel must be named explicitly."""
    seen: list[list[str]] = []
    monkeypatch.setattr("track.report._resolve_hotline_say", lambda: "/bin/hotline-say")
    monkeypatch.setattr(
        "track.report.subprocess.run",
        lambda cmd, **kw: seen.append(cmd) or _ok(),
    )
    monkeypatch.setenv("TRACK_HOTLINE_AGENT", "env-agent")

    post_summary("hi", agent="assignment-agent")
    assert seen[0][1:3] == ["--agent", "assignment-agent"]


def test_channel_env_beats_agent_env(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("track.report._resolve_hotline_say", lambda: "/bin/hotline-say")
    monkeypatch.setattr(
        "track.report.subprocess.run", lambda cmd, **kw: seen.append(cmd) or _ok()
    )
    monkeypatch.setenv("TRACK_HOTLINE_CHANNEL", "12345")
    monkeypatch.setenv("TRACK_HOTLINE_AGENT", "env-agent")

    post_summary("hi")
    assert seen[0][1:3] == ["--channel", "12345"]


def test_env_agent_is_the_fallback(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("track.report._resolve_hotline_say", lambda: "/bin/hotline-say")
    monkeypatch.setattr(
        "track.report.subprocess.run", lambda cmd, **kw: seen.append(cmd) or _ok()
    )
    monkeypatch.delenv("TRACK_HOTLINE_CHANNEL", raising=False)
    monkeypatch.setenv("TRACK_HOTLINE_AGENT", "env-agent")

    post_summary("hi")
    assert seen[0][1:3] == ["--agent", "env-agent"]


def test_with_no_target_hotline_say_resolves_the_session_itself(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("track.report._resolve_hotline_say", lambda: "/bin/hotline-say")
    monkeypatch.setattr(
        "track.report.subprocess.run", lambda cmd, **kw: seen.append(cmd) or _ok()
    )
    monkeypatch.delenv("TRACK_HOTLINE_CHANNEL", raising=False)
    monkeypatch.delenv("TRACK_HOTLINE_AGENT", raising=False)

    post_summary("hi")
    assert seen[0] == ["/bin/hotline-say", "hi"]


def test_a_failed_post_is_a_report_error(monkeypatch) -> None:
    monkeypatch.setattr("track.report._resolve_hotline_say", lambda: "/bin/hotline-say")
    monkeypatch.setattr(
        "track.report.subprocess.run",
        lambda cmd, **kw: _ok(returncode=1, stderr="no channel for this session"),
    )
    with pytest.raises(ReportError, match="no channel for this session"):
        post_summary("hi")


def test_a_missing_hotline_say_is_a_report_error(monkeypatch) -> None:
    monkeypatch.setattr("track.report.shutil.which", lambda _n: None)
    monkeypatch.setattr("track.report._FALLBACK_PATH", __import__("pathlib").Path("/nope"))
    with pytest.raises(ReportError, match="not found"):
        post_summary("hi")


def _ok(returncode: int = 0, stderr: str = ""):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)
