from __future__ import annotations

import json
from pathlib import Path

import pytest
import web_support

from track.web._cli import main


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    return web_support.seed_legacy(tmp_path)


@pytest.fixture
def future_db(tmp_path: Path) -> Path:
    return web_support.seed_future(tmp_path)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "track" in capsys.readouterr().out


def test_unknown_flag_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--nope"]) == 2
    assert "error" in capsys.readouterr().err


def test_missing_database_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["info", "--db", str(tmp_path / "nope.db")]) == 1
    assert "no database" in capsys.readouterr().err


def test_info_names_every_missing_field(
    legacy_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["info", "--db", str(legacy_db)]) == 0
    out = capsys.readouterr().out
    assert "-- absent --" in out
    assert "why it was recommended" in out
    assert "3 distinct listings" in out


def test_info_json(future_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["info", "--db", str(future_db), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["resolved"]["reason"] == "rationale"
    assert data["gaps"] == []
    assert data["listings"] == 3
    assert data["without_price"] == 1


def test_global_flags_work_after_the_subcommand(
    legacy_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    assert main(["info", "--db", str(legacy_db), "-q"]) == 0
    assert main(["-q", "info", "--db", str(legacy_db)]) == 0
