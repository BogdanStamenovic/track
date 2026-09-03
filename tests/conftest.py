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
