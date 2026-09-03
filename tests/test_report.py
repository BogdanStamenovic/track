from __future__ import annotations

import subprocess

import pytest

from track.errors import ReportError
from track.models import Assignment, Finding
from track.report import build_summary, post_summary


def _assignment(**overrides) -> Assignment:
    base = {
        "id": "a1",
        "text": "a cheap laptop",
        "interval_seconds": 3600,
        "status": "active",
        "max_price": None,
        "created_at": "now",
        "last_run_at": None,
        "next_run_at": None,
        "job_id": None,
        "backend": None,
    }
    base.update(overrides)
    return Assignment(**base)


def _finding(**overrides) -> Finding:
    base = {
        "id": 1,
        "assignment_id": "a1",
        "run_id": 1,
        "source": "eBay",
        "title": "ThinkPad",
        "price": 300.0,
        "currency": "USD",
        "url": "https://x",
        "dedup_key": "k",
        "score": 0.9,
        "is_new": True,
        "found_at": "now",
    }
    base.update(overrides)
    return Finding(**base)


def test_build_summary_no_new_findings() -> None:
    summary = build_summary(_assignment(), [], sources_checked=3)
    assert "no new listings" in summary
    assert "3 sources checked" in summary


def test_build_summary_lists_new_findings_ranked_by_score() -> None:
    low = _finding(id=1, title="cheap-ish", score=0.4)
    high = _finding(id=2, title="steal", score=0.95)
    summary = build_summary(_assignment(), [low, high], sources_checked=2)
    assert summary.index("steal") < summary.index("cheap-ish")


def test_build_summary_ignores_non_new_findings() -> None:
    old = _finding(is_new=False, title="already seen")
    summary = build_summary(_assignment(), [old], sources_checked=1)
    assert "already seen" not in summary
    assert "no new listings" in summary


def test_build_summary_truncates_and_counts_extra() -> None:
    findings = [_finding(id=i, title=f"item{i}", score=i / 10) for i in range(8)]
    summary = build_summary(_assignment(), findings, sources_checked=1)
    assert "and 3 more" in summary


def test_post_summary_missing_binary_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("track.report.shutil.which", lambda _b: None)
    monkeypatch.setattr("track.report._FALLBACK_PATH", tmp_path / "no-such-hotline-say")
    with pytest.raises(ReportError, match="not found"):
        post_summary("hi")


def test_post_summary_invokes_hotline_say(monkeypatch) -> None:
    monkeypatch.setattr("track.report.shutil.which", lambda _b: "/usr/bin/hotline-say")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("track.report.subprocess.run", fake_run)
    post_summary("hello world", agent="track-dev")
    assert calls == [["/usr/bin/hotline-say", "--agent", "track-dev", "hello world"]]


def test_post_summary_nonzero_exit_raises(monkeypatch) -> None:
    monkeypatch.setattr("track.report.shutil.which", lambda _b: "/usr/bin/hotline-say")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="discord down")

    monkeypatch.setattr("track.report.subprocess.run", fake_run)
    with pytest.raises(ReportError, match="discord down"):
        post_summary("hello")
