from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from track.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "track.db") as s:
        yield s


def envelope(result: str, *, cost: float = 0.01, is_error: bool = False) -> str:
    """What `claude -p --output-format json` actually writes to stdout."""
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": is_error,
         "result": result, "total_cost_usd": cost}
    )


def completed(
    stdout: str, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def no_subprocesses(monkeypatch, request):
    """No test may shell out. Ever.

    This exists because a test did. Wiring the reaper into `run_assignment`
    silently gave every engine test a real `claude -p` re-check scout: the
    suite went from 0.85s to 39s and spent live model usage on a `pytest`
    run, and nothing failed to say so. A stub in one fixture would have
    fixed that one case; this fixes the class of it.

    Tests that legitimately drive the subprocess boundary opt back in with
    `@pytest.mark.subprocess`, and there should be very few of them.
    """
    if "subprocess" in request.keywords:
        return

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            f"a test tried to run a subprocess: {args[0] if args else kwargs}. "
            "Fake it, or mark the test with @pytest.mark.subprocess."
        )

    monkeypatch.setattr(subprocess, "run", forbidden)
