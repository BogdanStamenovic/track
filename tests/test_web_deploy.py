"""The installer's contract, exercised rather than described.

The failure this file exists to prevent is a hang: `ownbox` runs setup commands
with a 1800s timeout and an inherited stdin, so a prompt with nobody there does
not fail fast -- it blocks for half an hour. Every test here drives the real
script with stdin closed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A checkout whose project.conf points everything at tmp_path."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "deploy").mkdir()
    for name in ("install.sh", "uninstall.sh", "update.sh", "track-web.service.in"):
        (root / "deploy" / name).write_bytes((DEPLOY / name).read_bytes())
    home = tmp_path / "home"
    (root / "deploy" / "project.conf").write_text(
        f'SERVICE_NAME="track-web-test.service"\n'
        f'UNIT_DIR="{home}/.config/systemd/user"\n'
        f'UNIT_FILE="${{UNIT_DIR}}/${{SERVICE_NAME}}"\n'
        f'CONFIG_DIR="{home}/.config/track"\n'
        f'ENV_FILE="${{CONFIG_DIR}}/web.env"\n'
        f'DATA_DIR="{home}/.local/share/track"\n'
        f"DEFAULT_PORT=8899\n"
    )
    # The venv step is the slow part and is not what these tests are about.
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "stub"\nversion = "0"\n'
    )
    return root


def run(root: Path, script: str, *args: str, env: dict[str, str] | None = None,
        timeout: float = 240) -> subprocess.CompletedProcess[str]:
    """Always with stdin closed: that is the condition under test."""
    full = {**os.environ, "TRACK_INSTALL_SKIP_VENV": "1", **(env or {})}
    with open(os.devnull) as devnull:
        return subprocess.run(
            ["bash", f"deploy/{script}", *args],
            cwd=root, stdin=devnull, capture_output=True, text=True,
            timeout=timeout, env=full, check=False,
        )


def env_file(root: Path) -> Path:
    conf = (root / "deploy" / "project.conf").read_text()
    line = next(ln for ln in conf.splitlines() if ln.startswith('CONFIG_DIR='))
    return Path(line.split('"')[1]) / "web.env"


@pytest.mark.subprocess
def test_no_tty_takes_defaults_instead_of_hanging(sandbox: Path) -> None:
    result = run(sandbox, "install.sh")
    assert result.returncode == 0, result.stderr
    assert "no terminal; using the default" in result.stderr
    assert env_file(sandbox).exists()


@pytest.mark.subprocess
def test_default_bind_is_localhost(sandbox: Path) -> None:
    run(sandbox, "install.sh")
    assert "TRACK_WEB_HOSTS=127.0.0.1" in env_file(sandbox).read_text()


@pytest.mark.subprocess
def test_env_vars_answer_every_question(sandbox: Path) -> None:
    result = run(sandbox, "install.sh", env={
        "TRACK_WEB": "yes", "TRACK_WEB_PORT": "9911", "TRACK_WEB_BIND": "open",
    })
    assert result.returncode == 0, result.stderr
    written = env_file(sandbox).read_text()
    assert "TRACK_WEB_PORT=9911" in written
    assert "TRACK_WEB_HOSTS=0.0.0.0" in written


@pytest.mark.subprocess
def test_declining_the_web_view_writes_no_config(sandbox: Path) -> None:
    result = run(sandbox, "install.sh", env={"TRACK_WEB": "no"})
    assert result.returncode == 0, result.stderr
    assert not env_file(sandbox).exists()


@pytest.mark.subprocess
def test_a_rerun_keeps_an_existing_config(sandbox: Path) -> None:
    run(sandbox, "install.sh", env={"TRACK_WEB_PORT": "9911"})
    before = env_file(sandbox).read_text()
    result = run(sandbox, "install.sh", env={"TRACK_WEB_PORT": "7777"})
    assert "Keeping the existing" in result.stderr
    assert env_file(sandbox).read_text() == before, "a re-run must not clobber config"


@pytest.mark.subprocess
def test_force_rewrites_it(sandbox: Path) -> None:
    run(sandbox, "install.sh", env={"TRACK_WEB_PORT": "9911"})
    run(sandbox, "install.sh", env={"TRACK_WEB_PORT": "7777", "TRACK_INSTALL_FORCE": "1"})
    assert "TRACK_WEB_PORT=7777" in env_file(sandbox).read_text()


@pytest.mark.subprocess
def test_a_bad_port_is_refused(sandbox: Path) -> None:
    result = run(sandbox, "install.sh", env={"TRACK_WEB_PORT": "eight"})
    assert result.returncode == 2
    assert "Not a port number" in result.stderr


@pytest.mark.subprocess
def test_the_unit_is_rendered_with_no_placeholders_left(sandbox: Path) -> None:
    run(sandbox, "install.sh")
    conf = (sandbox / "deploy" / "project.conf").read_text()
    unit = Path(next(ln for ln in conf.splitlines()
                     if ln.startswith('UNIT_DIR=')).split('"')[1]) / "track-web-test.service"
    text = unit.read_text()
    assert "@" not in text.replace("@PLACEHOLDERS@", ""), text
    assert "web serve" in text
    assert "EnvironmentFile=" in text


@pytest.mark.subprocess
def test_uninstall_removes_what_install_made_and_repeats_safely(sandbox: Path) -> None:
    run(sandbox, "install.sh")
    assert env_file(sandbox).exists()

    first = run(sandbox, "uninstall.sh")
    assert first.returncode == 0, first.stderr
    assert not env_file(sandbox).exists()

    second = run(sandbox, "uninstall.sh")
    assert second.returncode == 0, "uninstall must be safe to run twice"


@pytest.mark.subprocess
def test_uninstall_keeps_the_findings_unless_purged(sandbox: Path) -> None:
    conf = (sandbox / "deploy" / "project.conf").read_text()
    data = Path(next(ln for ln in conf.splitlines() if ln.startswith('DATA_DIR=')).split('"')[1])
    data.mkdir(parents=True)
    (data / "track.db").write_text("pretend findings")

    run(sandbox, "install.sh")
    run(sandbox, "uninstall.sh")
    assert (data / "track.db").exists(), "a plain uninstall must not delete the database"

    run(sandbox, "uninstall.sh", "--purge")
    assert not data.exists()
