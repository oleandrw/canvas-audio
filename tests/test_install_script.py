"""scripts/install.py, the double-click installer. Nothing is installed for real."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_script", ROOT / "scripts" / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "VENV", tmp_path / ".venv")
    module.calls = {"create": [], "run": []}
    module.pip_exit = 0

    def fake_create(path: Path) -> None:
        module.calls["create"].append(path)
        python = module.venv_python()
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("")

    def fake_run(cmd: list[str], cwd: Path | None = None) -> int:
        module.calls["run"].append(cmd)
        return module.pip_exit if "pip" in cmd else 0

    monkeypatch.setattr(module, "create_venv", fake_create)
    monkeypatch.setattr(module, "run", fake_run)
    return module


def pip_calls(module: ModuleType) -> list[list[str]]:
    return [cmd for cmd in module.calls["run"] if "pip" in cmd]


def shortcut_calls(module: ModuleType) -> list[list[str]]:
    return [
        cmd
        for cmd in module.calls["run"]
        if any(part.endswith("create-desktop-shortcut.ps1") for part in cmd)
    ]


class TestInstall:
    def test_creates_the_venv_and_installs_without_dev_tools(self, installer: ModuleType) -> None:
        assert installer.main(["--no-shortcut"]) == 0
        assert len(installer.calls["create"]) == 1
        (pip,) = pip_calls(installer)
        assert pip[-2:] == ["-e", "."]

    def test_dev_flag_also_installs_test_and_lint_tools(self, installer: ModuleType) -> None:
        assert installer.main(["--dev", "--no-shortcut"]) == 0
        (pip,) = pip_calls(installer)
        assert pip[-2:] == ["-e", ".[dev]"]

    def test_an_existing_venv_is_reused(self, installer: ModuleType) -> None:
        python = installer.venv_python()
        python.parent.mkdir(parents=True)
        python.write_text("")
        assert installer.main(["--no-shortcut"]) == 0
        assert installer.calls["create"] == []
        assert len(pip_calls(installer)) == 1

    def test_too_old_python_stops_before_touching_anything(
        self, installer: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(installer, "MIN_VERSION", (99, 0))
        assert installer.main(["--no-shortcut"]) == 1
        assert installer.calls == {"create": [], "run": []}
        assert "99.0" in capsys.readouterr().out

    def test_a_failed_install_is_reported(self, installer: ModuleType, capsys) -> None:
        installer.pip_exit = 1
        assert installer.main(["--no-shortcut"]) == 1
        assert "feilet" in capsys.readouterr().out


class TestShortcut:
    def test_made_only_when_asked_for(
        self, installer: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer, "IS_WINDOWS", True)
        assert installer.main(["--shortcut"]) == 0
        (cmd,) = shortcut_calls(installer)
        assert "-ExecutionPolicy" in cmd and "Bypass" in cmd

    def test_skipped_with_no_shortcut(
        self, installer: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer, "IS_WINDOWS", True)
        assert installer.main(["--no-shortcut"]) == 0
        assert shortcut_calls(installer) == []

    def test_no_question_without_an_interactive_console(
        self, installer: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Under pytest stdin is not a terminal, so the answer defaults to no.
        monkeypatch.setattr(installer, "IS_WINDOWS", True)
        assert installer.main([]) == 0
        assert shortcut_calls(installer) == []
