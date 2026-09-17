"""Checking for and applying updates to the app's own source code.

Every git call lives here, in one place: an argument list, never a shell, always
the project folder, always a timeout, and with credential prompts turned off so a
private remote cannot leave a request hanging on a login box. **No value from an
HTTP request ever reaches a git argument** — the branch and the remote are read
from the repository itself.

Only fast-forward is allowed, and only when the tracked files are clean, so an
update can never overwrite local work. Untracked files (the SQLite WAL files, for
instance) are reported but never block anything. The app does not restart itself:
it runs in a console window with no supervisor, so it tells the user instead.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT
from app.database import run_db
from app.logging_config import get_logger
from app.services import app_settings

logger = get_logger("app.services.update")

#: A module constant so tests and manual runs can point somewhere else.
WORK_TREE: Path = PROJECT_ROOT
GIT_TIMEOUT = 15.0
FETCH_TIMEOUT = 90.0
COMMIT_LIMIT = 20
RECENT_LIMIT = 5

#: A change to one of these means "run install.bat before starting again".
DEPENDENCY_FILES = frozenset(
    {"pyproject.toml", "requirements.txt", "install.bat", "scripts/install.py"}
)

_SEP = "\x1f"
_LOG_FORMAT = f"--format=%h{_SEP}%s{_SEP}%cd"
_DATE_FORMAT = "--date=format:%d.%m.%Y %H:%M"
_CREDENTIALS = re.compile(r"://[^/\s:@]+(?::[^/\s@]*)?@")
_AUTH_HINTS = ("could not read username", "authentication failed", "terminal prompts disabled")

MISSING_GIT = "Fant ikke git. Installer Git for Windows, eller hent ny kode manuelt."
NOT_A_REPO = (
    "Denne mappa er ikke hentet med git, så appen kan ikke oppdatere seg selv. "
    "Hent prosjektet på nytt med «git clone», eller last ned ny versjon manuelt."
)
NEEDS_LOGIN = (
    "Fikk ikke kontakt med origin: fjernlageret krever en innlogging som ikke er lagret på "
    "maskinen. Kjør «git fetch» i prosjektmappa én gang og lagre innloggingen."
)


def scrub(text: str) -> str:
    """Never show a password or token that happens to sit in a remote URL."""
    return _CREDENTIALS.sub("://***@", text or "")


@dataclass(slots=True)
class GitRun:
    ok: bool
    out: str = ""
    err: str = ""
    code: int = 0


@dataclass(slots=True)
class Commit:
    sha: str = ""
    subject: str = ""
    date: str = ""


def _run_git(args: list[str], *, timeout: float = GIT_TIMEOUT) -> GitRun:
    """The one place this application starts a process."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # never block waiting for a login
        "GCM_INTERACTIVE": "never",  # ... and never pop up Git Credential Manager
        "GIT_OPTIONAL_LOCKS": "0",
    }
    extra: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover - platform specific
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW  # no console flash
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=WORK_TREE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
            **extra,
        )
    except FileNotFoundError:
        return GitRun(False, err=MISSING_GIT, code=-1)
    except subprocess.TimeoutExpired:
        return GitRun(
            False, err=f"Git svarte ikke innen {int(timeout)} sekunder og ble avbrutt.", code=-1
        )
    return GitRun(
        ok=done.returncode == 0,
        out=scrub((done.stdout or "").strip()),
        err=scrub((done.stderr or "").strip()),
        code=done.returncode,
    )


async def _git(args: list[str], *, timeout: float = GIT_TIMEOUT) -> GitRun:
    return await asyncio.to_thread(_run_git, args, timeout=timeout)


def _commits(text: str) -> list[Commit]:
    commits: list[Commit] = []
    for line in text.splitlines():
        parts = line.split(_SEP)
        if len(parts) == 3:
            commits.append(Commit(*parts))
    return commits


def _last_fetch() -> str:
    try:
        stamp = (WORK_TREE / ".git" / "FETCH_HEAD").stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp).strftime("%d.%m.%Y %H:%M")


# --------------------------------------------------------------------------- #
# Reading the repository
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GitStatus:
    available: bool = True
    is_repo: bool = False
    branch: str = ""
    detached: bool = False
    head: Commit | None = None
    tracked_changes: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    has_upstream: bool = False
    ahead: int = 0
    behind: int = 0
    incoming: list[Commit] = field(default_factory=list)
    recent: list[Commit] = field(default_factory=list)
    last_fetch: str = ""
    error: str = ""

    @property
    def blocked_reason(self) -> str:
        """Why an update cannot run right now, in one sentence. Empty when it can."""
        if not self.available:
            return self.error or MISSING_GIT
        if not self.is_repo:
            return NOT_A_REPO
        if self.detached:
            return "HEAD peker ikke på en gren. Kjør «git switch main» i prosjektmappa først."
        if not self.has_upstream:
            return f"Grenen «{self.branch}» har ingen oppstrømsgren hos origin."
        if self.tracked_changes:
            return (
                f"Det finnes lokale endringer i {len(self.tracked_changes)} sporede filer. "
                "Oppdateringen stopper heller enn å overskrive dem."
            )
        if self.ahead:
            return (
                f"Den lokale grenen har {self.ahead} egne commits som ikke ligger hos origin, "
                "så «fast forward» er ikke mulig."
            )
        return ""

    @property
    def can_update(self) -> bool:
        return not self.blocked_reason and self.behind > 0


async def git_status() -> GitStatus:
    """Everything the page needs about the checkout. Never raises."""
    inside = await _git(["rev-parse", "--is-inside-work-tree"])
    if inside.code == -1:
        return GitStatus(available=False, error=inside.err)
    if not inside.ok or inside.out.strip() != "true":
        return GitStatus(is_repo=False, error=NOT_A_REPO)

    status = GitStatus(is_repo=True, last_fetch=_last_fetch())

    branch = await _git(["rev-parse", "--abbrev-ref", "HEAD"])
    status.branch = branch.out or "?"
    status.detached = status.branch == "HEAD"

    head = await _git(["log", "-1", _DATE_FORMAT, _LOG_FORMAT])
    status.head = next(iter(_commits(head.out)), None)

    tracked = await _git(["status", "--porcelain", "--untracked-files=no"])
    status.tracked_changes = [line.strip() for line in tracked.out.splitlines() if line.strip()]
    untracked = await _git(["ls-files", "--others", "--exclude-standard"])
    status.untracked = [line for line in untracked.out.splitlines() if line]

    recent = await _git(["log", f"--max-count={RECENT_LIMIT}", _DATE_FORMAT, _LOG_FORMAT])
    status.recent = _commits(recent.out)

    if status.detached:
        return status

    upstream = await _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    status.has_upstream = upstream.ok
    if not upstream.ok:
        return status

    counts = await _git(["rev-list", "--count", "--left-right", f"HEAD...origin/{status.branch}"])
    ahead, _, behind = counts.out.partition("\t")
    status.ahead = int(ahead.strip()) if ahead.strip().isdigit() else 0
    status.behind = int(behind.strip()) if behind.strip().isdigit() else 0
    if status.behind:
        incoming = await _git(
            [
                "log",
                f"--max-count={COMMIT_LIMIT}",
                _DATE_FORMAT,
                _LOG_FORMAT,
                f"HEAD..origin/{status.branch}",
            ]
        )
        status.incoming = _commits(incoming.out)
    return status


_boot_commit: str | None = None
_restart_pending = False


async def boot_commit() -> str:
    """The commit the running process started from. Read once, then remembered."""
    global _boot_commit
    if _boot_commit is None:
        run = await _git(["rev-parse", "--short", "HEAD"])
        _boot_commit = run.out if run.ok else ""
    return _boot_commit


def restart_pending() -> bool:
    """True once an update has been applied in this process but not yet taken in use."""
    return _restart_pending


def reset_state() -> None:
    """Forget the cached commit and the restart flag. Used by tests."""
    global _boot_commit, _restart_pending
    _boot_commit = None
    _restart_pending = False


# --------------------------------------------------------------------------- #
# Changing the repository
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CheckResult:
    ok: bool
    message: str = ""
    behind: int = 0
    incoming: list[Commit] = field(default_factory=list)


@dataclass(slots=True)
class UpdateResult:
    ok: bool
    updated: bool = False
    message: str = ""
    before: str = ""
    after: str = ""
    commits: list[Commit] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    needs_install: bool = False
    restart_required: bool = False


def _fetch_error(run: GitRun) -> str:
    lowered = run.err.lower()
    if any(hint in lowered for hint in _AUTH_HINTS):
        return NEEDS_LOGIN
    if run.code == -1:
        return run.err
    return f"Fikk ikke hentet fra origin. Sjekk nettforbindelsen. {run.err}".strip()


_lock = asyncio.Lock()


async def check_for_updates() -> CheckResult:
    """Ask origin what is new. Changes nothing in the working tree."""
    status = await git_status()
    if not status.available or not status.is_repo or status.detached:
        return CheckResult(False, status.blocked_reason)

    fetched = await _git(["fetch", "--quiet", "origin", status.branch], timeout=FETCH_TIMEOUT)
    if not fetched.ok:
        return CheckResult(False, _fetch_error(fetched))

    after = await git_status()
    if after.behind:
        noun = "commit" if after.behind == 1 else "commits"
        return CheckResult(
            True, f"{after.behind} nye {noun} er klare.", after.behind, after.incoming
        )
    return CheckResult(True, "Du har siste versjon.")


async def apply_update() -> UpdateResult:
    """Fetch and fast-forward. Refuses rather than overwriting anything."""
    global _restart_pending
    if _lock.locked():
        return UpdateResult(False, message="En oppdatering kjører allerede.")

    async with _lock:
        status = await git_status()
        if status.blocked_reason:
            return UpdateResult(False, message=status.blocked_reason)

        fetched = await _git(["fetch", "--quiet", "origin", status.branch], timeout=FETCH_TIMEOUT)
        if not fetched.ok:
            return UpdateResult(False, message=_fetch_error(fetched))

        status = await git_status()
        if status.blocked_reason:
            return UpdateResult(False, message=status.blocked_reason)

        before = status.head.sha if status.head else ""
        if not status.behind:
            return UpdateResult(True, message=f"Du har allerede siste versjon ({before}).")

        merged = await _git(["merge", "--ff-only", f"origin/{status.branch}"])
        if not merged.ok:
            message = (
                f"Kunne ikke oppdatere automatisk («fast forward» var ikke mulig). {merged.err}"
            ).strip()
            await _record(before, before, ok=False, message=message)
            return UpdateResult(False, message=message)

        head = await _git(["rev-parse", "--short", "HEAD"])
        after = head.out
        diff = (
            await _git(["diff", "--name-only", before, after]) if before and after else GitRun(True)
        )
        changed = [line for line in diff.out.splitlines() if line]
        needs_install = any(name in DEPENDENCY_FILES for name in changed)

        _restart_pending = True
        count = len(status.incoming) or status.behind
        noun = "commit" if count == 1 else "commits"
        message = f"Hentet {count} {noun}. Start appen på nytt for å ta den nye koden i bruk."
        if needs_install:
            message += " Avhengighetene er endret, så kjør install.bat først."

        await _record(before, after, ok=True, message=message)
        logger.info("Updated %s -> %s (%d files changed)", before, after, len(changed))
        return UpdateResult(
            ok=True,
            updated=True,
            message=message,
            before=before,
            after=after,
            commits=status.incoming,
            changed_files=changed,
            needs_install=needs_install,
            restart_required=True,
        )


async def _record(before: str, after: str, *, ok: bool, message: str) -> None:
    record = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "from_sha": before,
        "to_sha": after,
        "ok": ok,
        "message": message,
    }
    await run_db(lambda session: app_settings.record_last_update(session, record))
