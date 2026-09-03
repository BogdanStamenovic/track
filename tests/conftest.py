from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from track.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "track.db") as s:
        yield s
