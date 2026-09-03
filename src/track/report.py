"""Builds a run summary and posts it to Discord via hotline-say."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import ReportError
from .models import Assignment, Finding

HOTLINE_SAY_BIN = "hotline-say"
_FALLBACK_PATH = Path.home() / ".claude" / "bin" / HOTLINE_SAY_BIN


def build_summary(assignment: Assignment, findings: list[Finding], sources_checked: int) -> str:
    new_findings = [f for f in findings if f.is_new]
    lines = [
        (
            f'track: "{assignment.text}" '
            f"({sources_checked} sources checked, {len(findings)} listings seen)"
        )
    ]
    if not new_findings:
        lines.append("no new listings this run.")
    else:
        ranked = sorted(new_findings, key=lambda f: (f.score or 0), reverse=True)
        for f in ranked[:5]:
            price = f"{f.price:.2f} {f.currency}" if f.price is not None else "price unknown"
            score_tag = f" (score {f.score:.2f})" if f.score is not None else ""
            lines.append(f"- {f.title} — {price} @ {f.source}{score_tag}")
            if f.url:
                lines.append(f"  {f.url}")
        if len(ranked) > 5:
            lines.append(f"...and {len(ranked) - 5} more new listings.")
    return "\n".join(lines)


def _resolve_hotline_say() -> str:
    found = shutil.which(HOTLINE_SAY_BIN)
    if found:
        return found
    if _FALLBACK_PATH.exists():
        return str(_FALLBACK_PATH)
    raise ReportError(f"{HOTLINE_SAY_BIN} not found on PATH or at {_FALLBACK_PATH}")


def post_summary(summary: str, *, agent: str | None = None) -> None:
    binary = _resolve_hotline_say()
    cmd = [binary]
    if agent:
        cmd += ["--agent", agent]
    cmd.append(summary)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise ReportError(f"hotline-say exited {result.returncode}: {result.stderr.strip()[:500]}")
