"""Updating the app itself. Git is faked here; the suite never touches a real checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.services import update_service as svc

SEP = svc._SEP


def ok(out: str = "") -> svc.GitRun:
    return svc.GitRun(True, out, "", 0)


def fail(err: str = "", code: int = 1) -> svc.GitRun:
    return svc.GitRun(False, "", err, code)


def line(sha: str, subject: str = "Ny funksjon", date: str = "15.09.2026 10:00") -> str:
    return f"{sha}{SEP}{subject}{SEP}{date}"


def repo(
    overrides: dict[tuple[str, ...], svc.GitRun] | None = None,
) -> dict[tuple[str, ...], svc.GitRun]:
    """A healthy checkout on main, level with origin. Override one command per test."""
    table: dict[tuple[str, ...], svc.GitRun] = {
        ("rev-parse", "--is-inside-work-tree"): ok("true"),
        ("rev-parse", "--abbrev-ref", "HEAD"): ok("main"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name"): ok("origin/main"),
        ("rev-parse", "--short", "HEAD"): ok("abc1234"),
        ("log", "-1"): ok(line("abc1234", "Forrige commit")),
        ("log", "--max-count=5"): ok(line("abc1234", "Forrige commit")),
        ("log", "--max-count=20"): ok(""),
        ("status", "--porcelain"): ok(""),
        ("ls-files", "--others"): ok(""),
        ("rev-list", "--count", "--left-right"): ok("0\t0"),
        ("fetch",): ok(""),
        ("merge", "--ff-only"): ok("Fast-forward"),
        ("diff", "--name-only"): ok(""),
    }
    table.update(overrides or {})
    return table


def behind_by(
    count: int, overrides: dict[tuple[str, ...], svc.GitRun] | None = None
) -> dict[tuple[str, ...], svc.GitRun]:
    subjects = [line(f"new{i}", f"Commit {i}") for i in range(count)]
    return repo(
        {
            ("rev-list", "--count", "--left-right"): ok(f"0\t{count}"),
            ("log", "--max-count=20"): ok("\n".join(subjects)),
            **(overrides or {}),
        }
    )


def fake_git(
    monkeypatch: pytest.MonkeyPatch,
    table: dict[tuple[str, ...], svc.GitRun],
    calls: list[list[str]] | None = None,
) -> None:
    def run(args: list[str], *, timeout: float = svc.GIT_TIMEOUT) -> svc.GitRun:
        if calls is not None:
            calls.append(list(args))
        for prefix, response in table.items():
            if tuple(args[: len(prefix)]) == prefix:
                return response
        return ok()

    monkeypatch.setattr(svc, "_run_git", run)


class TestReadingTheRepository:
    async def test_it_reports_branch_head_and_recent_commits(self, monkeypatch) -> None:
        fake_git(monkeypatch, repo())

        status = await svc.git_status()

        assert status.is_repo is True
        assert status.branch == "main"
        assert status.head is not None
        assert (status.head.sha, status.head.subject) == ("abc1234", "Forrige commit")
        assert [c.sha for c in status.recent] == ["abc1234"]
        assert status.blocked_reason == ""

    async def test_untracked_files_never_block_an_update(self, monkeypatch) -> None:
        """The SQLite WAL files sit untracked in data/ and must not stop anything."""
        table = behind_by(2, {("ls-files", "--others"): ok("data/canvas_audio.db-wal")})
        fake_git(monkeypatch, table)

        status = await svc.git_status()

        assert status.tracked_changes == []
        assert status.untracked == ["data/canvas_audio.db-wal"]
        assert status.can_update is True
        assert status.blocked_reason == ""

    async def test_tracked_changes_block_an_update(self, monkeypatch) -> None:
        fake_git(monkeypatch, behind_by(1, {("status", "--porcelain"): ok(" M app/main.py")}))

        status = await svc.git_status()

        assert status.can_update is False
        assert "lokale endringer" in status.blocked_reason

    @pytest.mark.parametrize(
        ("table", "fragment"),
        [
            (repo({("rev-parse", "--abbrev-ref", "HEAD"): ok("HEAD")}), "git switch"),
            (
                repo({("rev-parse", "--abbrev-ref", "--symbolic-full-name"): fail("no upstream")}),
                "oppstrømsgren",
            ),
            (repo({("rev-list", "--count", "--left-right"): ok("2\t1")}), "egne commits"),
        ],
        ids=["detached-head", "no-upstream", "commits-ahead"],
    )
    async def test_states_that_stop_a_fast_forward(
        self, monkeypatch, table: dict[tuple[str, ...], svc.GitRun], fragment: str
    ) -> None:
        fake_git(monkeypatch, table)
        status = await svc.git_status()
        assert status.can_update is False
        assert fragment in status.blocked_reason

    async def test_a_missing_git_binary_is_reported_in_norwegian(self, monkeypatch) -> None:
        fake_git(monkeypatch, {("rev-parse",): svc.GitRun(False, "", svc.MISSING_GIT, -1)})

        status = await svc.git_status()

        assert status.available is False
        assert "Fant ikke git" in status.blocked_reason

    async def test_a_folder_without_git_is_reported(self, monkeypatch) -> None:
        fake_git(
            monkeypatch, {("rev-parse", "--is-inside-work-tree"): fail("not a repository", 128)}
        )

        status = await svc.git_status()

        assert status.is_repo is False
        assert "git clone" in status.blocked_reason


class TestTheSubprocessCall:
    def _capture(self, monkeypatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(args, 0, "ok", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return seen

    def test_git_never_runs_through_a_shell(self, monkeypatch) -> None:
        seen = self._capture(monkeypatch)

        svc._run_git(["fetch", "--quiet", "origin", "main"], timeout=svc.FETCH_TIMEOUT)

        assert seen["args"] == ["git", "fetch", "--quiet", "origin", "main"]
        assert seen["kwargs"].get("shell") in (None, False)
        assert seen["kwargs"]["cwd"] == svc.WORK_TREE
        assert seen["kwargs"]["timeout"] == svc.FETCH_TIMEOUT
        assert seen["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["kwargs"]["env"]["GCM_INTERACTIVE"] == "never"

    def test_a_missing_binary_does_not_raise(self, monkeypatch) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise FileNotFoundError("git")

        monkeypatch.setattr(subprocess, "run", boom)

        run = svc._run_git(["status"])

        assert run.ok is False
        assert "Fant ikke git" in run.err

    def test_a_timeout_does_not_raise(self, monkeypatch) -> None:
        def slow(*_args: Any, **_kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=15)

        monkeypatch.setattr(subprocess, "run", slow)

        run = svc._run_git(["fetch"])

        assert run.ok is False
        assert "svarte ikke innen" in run.err

    def test_a_token_in_a_remote_url_is_scrubbed(self) -> None:
        text = "fatal: https://ole:ghp_hemmelig@github.com/oleandrw/canvas-audio.git not found"
        assert "ghp_hemmelig" not in svc.scrub(text)
        assert "://***@github.com" in svc.scrub(text)


class TestChecking:
    async def test_it_reports_what_is_new(self, monkeypatch) -> None:
        calls: list[list[str]] = []
        fake_git(monkeypatch, behind_by(3), calls)

        result = await svc.check_for_updates()

        assert result.ok is True
        assert result.behind == 3
        assert len(result.incoming) == 3
        assert ["fetch", "--quiet", "origin", "main"] in calls

    async def test_being_up_to_date_says_so(self, monkeypatch) -> None:
        fake_git(monkeypatch, repo())
        result = await svc.check_for_updates()
        assert (result.ok, result.behind) == (True, 0)
        assert "siste versjon" in result.message

    async def test_a_login_problem_gets_a_clear_message(self, monkeypatch) -> None:
        table = repo({("fetch",): fail("fatal: could not read Username for 'https://github.com'")})
        fake_git(monkeypatch, table)

        result = await svc.check_for_updates()

        assert result.ok is False
        assert "krever en innlogging" in result.message


class TestApplying:
    async def test_it_fast_forwards_and_reports_the_changed_files(
        self, env: Path, monkeypatch
    ) -> None:
        calls: list[list[str]] = []
        table = behind_by(
            2,
            {
                ("diff", "--name-only"): ok("app/main.py\napp/routes/ui.py"),
                ("rev-parse", "--short", "HEAD"): ok("def5678"),
            },
        )
        fake_git(monkeypatch, table, calls)

        result = await svc.apply_update()

        assert result.ok and result.updated
        assert result.changed_files == ["app/main.py", "app/routes/ui.py"]
        assert result.needs_install is False
        assert result.restart_required is True
        assert svc.restart_pending() is True
        assert ["merge", "--ff-only", "origin/main"] in calls
        assert "Start appen på nytt" in result.message

    async def test_it_says_when_dependencies_changed(self, env: Path, monkeypatch) -> None:
        table = behind_by(1, {("diff", "--name-only"): ok("pyproject.toml")})
        fake_git(monkeypatch, table)

        result = await svc.apply_update()

        assert result.needs_install is True
        assert "install.bat" in result.message

    async def test_a_dirty_tree_is_refused_without_merging(self, env: Path, monkeypatch) -> None:
        calls: list[list[str]] = []
        fake_git(
            monkeypatch, behind_by(1, {("status", "--porcelain"): ok(" M app/main.py")}), calls
        )

        result = await svc.apply_update()

        assert result.ok is False
        assert "lokale endringer" in result.message
        assert not any("merge" in call for call in calls)
        assert svc.restart_pending() is False

    async def test_a_diverged_branch_is_reported_not_forced(self, env: Path, monkeypatch) -> None:
        calls: list[list[str]] = []
        table = behind_by(1, {("merge", "--ff-only"): fail("fatal: Not possible to fast-forward")})
        fake_git(monkeypatch, table, calls)

        result = await svc.apply_update()

        assert result.ok is False
        assert "fast forward" in result.message
        flat = [arg for call in calls for arg in call]
        assert "--force" not in flat
        assert "reset" not in flat

    async def test_already_up_to_date_changes_nothing(self, env: Path, monkeypatch) -> None:
        calls: list[list[str]] = []
        fake_git(monkeypatch, repo(), calls)

        result = await svc.apply_update()

        assert result.ok is True
        assert result.updated is False
        assert "allerede siste versjon" in result.message
        assert not any("merge" in call for call in calls)

    async def test_the_result_is_remembered(self, env: Path, monkeypatch) -> None:
        from app.database import session_scope
        from app.services import app_settings

        fake_git(monkeypatch, behind_by(1))

        await svc.apply_update()

        with session_scope() as session:
            record = app_settings.read_last_update(session)
        assert record is not None
        assert record["ok"] is True
        assert record["from_sha"] == "abc1234"

    async def test_two_updates_cannot_run_at_once(self, env: Path, monkeypatch) -> None:
        fake_git(monkeypatch, behind_by(1))

        async with svc._lock:
            result = await svc.apply_update()

        assert result.ok is False
        assert "kjører allerede" in result.message
