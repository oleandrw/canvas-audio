"""Update page: what is running, what is new, and taking it in use.

The endpoints take no input at all — the branch and the remote come from the
repository (see :mod:`app.services.update_service`). Expected problems come back
as 200 with ``ok=false`` and a Norwegian message, so the page can show them
inline; only real bugs reach the global error handlers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import __version__
from app.database import SCHEMA_VERSION, file_schema_version, last_schema_state, run_db
from app.routes.ui import guard, render
from app.schemas.update import (
    CheckResultOut,
    CommitInfo,
    GitStatusOut,
    LastUpdateOut,
    RunningCodeOut,
    SchemaStatusOut,
    UpdateResultOut,
    UpdateStatusOut,
)
from app.security import require_auth
from app.services import app_settings, update_service

router = APIRouter(prefix="/api/update", tags=["update"], dependencies=[Depends(require_auth)])
page_router = APIRouter(tags=["ui"], include_in_schema=False)


def _human_time(value: str) -> str:
    """Stored as ISO for machines, shown the way a Norwegian reads a clock."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return moment.astimezone().strftime("%d.%m.%Y %H:%M")


def _commit(commit: update_service.Commit) -> CommitInfo:
    return CommitInfo(sha=commit.sha, subject=commit.subject, date=commit.date)


def _git_out(status: update_service.GitStatus) -> GitStatusOut:
    return GitStatusOut(
        available=status.available,
        is_repo=status.is_repo,
        branch=status.branch,
        detached=status.detached,
        head=_commit(status.head) if status.head else None,
        tracked_changes=status.tracked_changes,
        untracked=status.untracked,
        has_upstream=status.has_upstream,
        ahead=status.ahead,
        behind=status.behind,
        incoming=[_commit(c) for c in status.incoming],
        recent=[_commit(c) for c in status.recent],
        last_fetch=status.last_fetch,
        error=status.error,
        can_update=status.can_update,
        blocked_reason=status.blocked_reason,
    )


def _db_facts(session: Session) -> tuple[SchemaStatusOut, LastUpdateOut | None]:
    """The schema story: what the code expects, what startup did, what the file says."""
    stored = app_settings.read_schema_state(session)
    in_memory = last_schema_state()
    recorded = stored["version"] or (in_memory.version if in_memory else 0)
    added = stored["added_columns"] or (in_memory.added_columns if in_memory else [])
    file_version = file_schema_version()
    schema = SchemaStatusOut(
        expected_version=SCHEMA_VERSION,
        recorded_version=recorded,
        file_version=file_version,
        checked_at=_human_time(stored["checked_at"]),
        added_columns=added,
        in_sync=file_version in (None, SCHEMA_VERSION) and recorded in (0, SCHEMA_VERSION),
    )

    record = app_settings.read_last_update(session)
    last = (
        LastUpdateOut(
            at=_human_time(str(record.get("at", ""))),
            from_sha=str(record.get("from_sha", "")),
            to_sha=str(record.get("to_sha", "")),
            ok=bool(record.get("ok", True)),
            message=str(record.get("message", "")),
        )
        if record
        else None
    )
    return schema, last


async def collect_status() -> UpdateStatusOut:
    git = await update_service.git_status()
    boot = await update_service.boot_commit()
    head = git.head.sha if git.head else ""
    schema, last = await run_db(_db_facts)
    return UpdateStatusOut(
        running=RunningCodeOut(
            version=__version__,
            boot_commit=boot,
            head_commit=head,
            # Either this process applied an update, or someone did it from the
            # command line while the app was running.
            restart_pending=update_service.restart_pending()
            or bool(boot and head and boot != head),
        ),
        git=_git_out(git),
        db=schema,
        last_update=last,
    )


@router.get("/status", response_model=UpdateStatusOut)
async def update_status() -> UpdateStatusOut:
    return await collect_status()


@router.post("/check", response_model=CheckResultOut)
async def check_for_updates() -> CheckResultOut:
    result = await update_service.check_for_updates()
    return CheckResultOut(
        ok=result.ok,
        message=result.message,
        behind=result.behind,
        incoming=[_commit(c) for c in result.incoming],
    )


@router.post("/apply", response_model=UpdateResultOut)
async def apply_update() -> UpdateResultOut:
    result = await update_service.apply_update()
    return UpdateResultOut(
        ok=result.ok,
        updated=result.updated,
        message=result.message,
        before=result.before,
        after=result.after,
        commits=[_commit(c) for c in result.commits],
        changed_files=result.changed_files,
        needs_install=result.needs_install,
        restart_required=result.restart_required,
    )


@page_router.get("/oppdatering", response_class=HTMLResponse)
async def update_view(request: Request) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect

    return render(request, "update.html", "update", {"status": await collect_status()})
