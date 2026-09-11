"""First-time setup, run by install.bat (double-click on Windows).

Creates the virtual environment in .venv (or reuses one), installs the app into
it, and can put a "Canvas til lyd" shortcut on the desktop. It needs nothing but
Python 3.11 or newer. No PowerShell script is run except the shortcut helper,
which is started with -ExecutionPolicy Bypass for that one run only, so the
machine's execution policy is never changed.

    python scripts/install.py [--dev] [--shortcut | --no-shortcut]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
MIN_VERSION = (3, 11)
IS_WINDOWS = sys.platform == "win32"


def venv_python() -> Path:
    return VENV / "Scripts" / "python.exe" if IS_WINDOWS else VENV / "bin" / "python"


def create_venv(path: Path) -> None:
    venv.EnvBuilder(with_pip=True).create(path)


def run(cmd: list[str], cwd: Path | None = None) -> int:
    return subprocess.run(cmd, cwd=cwd, check=False).returncode


def ask(prompt: str) -> bool:
    """Yes/no question. Without an interactive console the answer is no."""
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in ("", "j", "ja", "y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Installer Canvas til lyd.")
    parser.add_argument(
        "--dev", action="store_true", help="Installer også test- og lintverktøyene."
    )
    shortcut_group = parser.add_mutually_exclusive_group()
    shortcut_group.add_argument("--shortcut", dest="shortcut", action="store_true", default=None)
    shortcut_group.add_argument("--no-shortcut", dest="shortcut", action="store_false")
    args = parser.parse_args(argv)

    version = ".".join(str(part) for part in MIN_VERSION)
    if sys.version_info[:2] < MIN_VERSION:
        found = sys.version.split()[0]
        print(f"Canvas til lyd trenger Python {version} eller nyere, men fant {found}.")
        print("Last ned en nyere versjon fra https://www.python.org/downloads/")
        print("og kjør install.bat på nytt.")
        return 1

    print("1/3  Virtuelt miljø (.venv)")
    if venv_python().exists():
        print("     Finnes allerede og gjenbrukes.")
    else:
        print("     Lager det nå ...")
        create_venv(VENV)

    too_old_check = f"import sys; sys.exit(0 if sys.version_info[:2] >= {MIN_VERSION} else 1)"
    if run([str(venv_python()), "-c", too_old_check]) != 0:
        print(f"     Mappen .venv er laget med en eldre Python enn {version}.")
        print("     Slett mappen .venv og kjør install.bat på nytt.")
        return 1

    print("2/3  Installerer Canvas til lyd. Første gang kan det ta noen minutter ...")
    target = ".[dev]" if args.dev else "."
    pip = [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-e", target]
    if run(pip, cwd=ROOT) != 0:
        print()
        print("Installasjonen feilet. Se meldingene over, sjekk nettforbindelsen og prøv igjen.")
        return 1

    print("3/3  Snarvei på skrivebordet")
    wants_shortcut = args.shortcut
    if wants_shortcut is None:
        wants_shortcut = ask("     Vil du ha ikonet «Canvas til lyd» på skrivebordet? [J/n] ")
    made_shortcut = False
    if wants_shortcut and IS_WINDOWS:
        script = ROOT / "scripts" / "create-desktop-shortcut.ps1"
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        made_shortcut = run(cmd) == 0
    elif not wants_shortcut:
        print("     Hoppet over.")

    print()
    where = "start.bat eller ikonet på skrivebordet" if made_shortcut else "start.bat"
    print(f"Ferdig! Start appen ved å dobbeltklikke {where}.")
    print("Første gang legger du inn Canvas- og ElevenLabs-nøklene under Innstillinger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
