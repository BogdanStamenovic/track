from __future__ import annotations

import sys
from pathlib import Path

from track.cli import _run_command, main
from track.scheduler import ScheduleResult


def test_run_command_falls_back_to_argv0_when_track_not_on_path(monkeypatch) -> None:
    monkeypatch.setattr("track.cli.shutil.which", lambda _b: None)
    monkeypatch.setattr(sys, "argv", ["/home/bodas/data/track/.venv/bin/track", "add", "x"])
    cmd = _run_command(["run", "abc123"])
    assert cmd == ["/home/bodas/data/track/.venv/bin/track", "run", "abc123"]


def test_run_command_prefers_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr("track.cli.shutil.which", lambda _b: "/usr/local/bin/track")
    cmd = _run_command(["run", "abc123"])
    assert cmd[0] == "/usr/local/bin/track"


def test_version(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "track" in out


def test_usage_error_exit_code(capsys) -> None:
    code = main([])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_invalid_interval_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "track.cli.schedule_wakeup",
        lambda *a, **k: ScheduleResult(job_id="j", backend="wake", next_run_at="t"),
    )
    code = main(["--db", str(tmp_path / "t.db"), "add", "x", "--interval", "notanumber"])
    assert code == 2
    assert "invalid --interval" in capsys.readouterr().err


def test_add_prints_id_and_schedules(tmp_path: Path, monkeypatch, capsys) -> None:
    calls = []

    def fake_schedule(assignment_id, interval_seconds, cmd, **kw):
        calls.append((assignment_id, interval_seconds))
        return ScheduleResult(job_id="job-1", backend="wake", next_run_at="soon")

    monkeypatch.setattr("track.cli.schedule_wakeup", fake_schedule)
    code = main(["--db", str(tmp_path / "t.db"), "add", "a cheap laptop", "--interval", "1h"])
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert len(out) == 8  # assignment id
    assert calls[0][1] == 3600


def test_add_no_schedule_skips_scheduling(tmp_path: Path, monkeypatch) -> None:
    def fail_schedule(*a, **k):
        raise AssertionError("should not schedule")

    monkeypatch.setattr("track.cli.schedule_wakeup", fail_schedule)
    code = main(["--db", str(tmp_path / "t.db"), "add", "x", "--no-schedule"])
    assert code == 0


def test_add_schedule_failure_still_succeeds(tmp_path: Path, monkeypatch, capsys) -> None:
    from track.errors import SchedulerError

    def fail_schedule(*a, **k):
        raise SchedulerError("wake not ready")

    monkeypatch.setattr("track.cli.schedule_wakeup", fail_schedule)
    code = main(["--db", str(tmp_path / "t.db"), "add", "x"])
    assert code == 0
    assert "wake not ready" in capsys.readouterr().err


def test_list_empty(tmp_path: Path, capsys) -> None:
    code = main(["--db", str(tmp_path / "t.db"), "list"])
    assert code == 0


def test_list_shows_assignments(tmp_path: Path, capsys) -> None:
    db = tmp_path / "t.db"
    main(["--db", str(db), "add", "laptops", "--no-schedule"])
    code = main(["--db", str(db), "list"])
    assert code == 0
    assert "laptops" in capsys.readouterr().out


def test_show_missing_assignment(tmp_path: Path, capsys) -> None:
    code = main(["--db", str(tmp_path / "t.db"), "show", "nope"])
    assert code == 1
    assert "no such assignment" in capsys.readouterr().err


def test_run_missing_assignment(tmp_path: Path, capsys) -> None:
    code = main(["--db", str(tmp_path / "t.db"), "run", "nope"])
    assert code == 1


def test_run_invokes_engine(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "t.db"
    main(["--db", str(db), "add", "laptops", "--no-schedule"])
    assignment_id = capsys.readouterr().out.strip()

    monkeypatch.setattr("track.cli.run_assignment", lambda store, a, **kw: ([], "summary text"))
    code = main(["--db", str(db), "run", assignment_id])
    assert code == 0
    assert "summary text" in capsys.readouterr().out


def test_remove_cancels_schedule_and_deletes(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "t.db"
    monkeypatch.setattr(
        "track.cli.schedule_wakeup",
        lambda *a, **k: ScheduleResult(job_id="job-1", backend="wake", next_run_at="soon"),
    )
    main(["--db", str(db), "add", "laptops"])
    assignment_id = capsys.readouterr().out.strip()

    cancelled = []
    monkeypatch.setattr("track.cli.cancel_schedule", lambda job_id, backend: cancelled.append((job_id, backend)))
    code = main(["--db", str(db), "remove", assignment_id])
    assert code == 0
    assert cancelled == [("job-1", "wake")]
    assert main(["--db", str(db), "show", assignment_id]) == 1


def test_pause_then_resume(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "t.db"
    monkeypatch.setattr(
        "track.cli.schedule_wakeup",
        lambda *a, **k: ScheduleResult(job_id="job-1", backend="wake", next_run_at="soon"),
    )
    main(["--db", str(db), "add", "laptops"])
    assignment_id = capsys.readouterr().out.strip()

    monkeypatch.setattr("track.cli.cancel_schedule", lambda job_id, backend: None)
    code = main(["--db", str(db), "pause", assignment_id])
    assert code == 0
    assert "paused" in capsys.readouterr().err

    code = main(["--db", str(db), "resume", assignment_id])
    assert code == 0
    assert "resumed" in capsys.readouterr().err
