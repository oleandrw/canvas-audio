"""Publishing generated audio into Canvas: a folder per module, a player per page.

For each page with up-to-date audio:

1. Upload the MP3 to Filer › <root folder>/<NN module name>, unless the same file
   is already there.
2. Fetch the page fresh from Canvas, put the player at the top (replacing an
   earlier one) and save it. Canvas keeps the previous version in the page history.

A page that fails is reported and the rest carry on. Running it again only does
what changed: a new audio file, a new design, or a player that went missing.
Removing takes the player off the pages and leaves the files in Canvas.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import run_db
from app.logging_config import get_logger
from app.models.core import CanvasEmbed, Course, Module, Page
from app.services.app_settings import get_value, set_value
from app.services.audio_service import resolve_path
from app.services.canvas_player import (
    PlayerDesign,
    has_player,
    player_html,
    player_src,
    strip_players,
    with_player,
)
from app.services.canvas_service import CanvasClient, CanvasPageBody
from app.services.errors import ConfigurationError, ServiceError
from app.services.hashing import audio_is_current
from app.services.presenters import selected_course
from app.services.sync_service import refresh_page_body

logger = get_logger("app.services.canvas_publish")

DEFAULT_FOLDER = "Lydfiler"
MAX_FOLDER_NAME = 100
_UNSAFE_FOLDER_CHARS = re.compile(r"[\\/\x00-\x1f]+")

ACTIONS = ("publish", "remove")

STATE_NO_AUDIO = "no_audio"
STATE_NOT_PUBLISHED = "not_published"
STATE_PUBLISHED = "published"
STATE_FILE_CHANGED = "file_changed"
STATE_DESIGN_CHANGED = "design_changed"
STATE_FAILED = "failed"

#: state -> (label, badge class)
STATE_LABELS: dict[str, tuple[str, str]] = {
    STATE_NO_AUDIO: ("Ingen oppdatert lyd", "badge-muted"),
    STATE_NOT_PUBLISHED: ("Ikke publisert", "badge-muted"),
    STATE_PUBLISHED: ("Publisert", "badge-ok"),
    STATE_FILE_CHANGED: ("Ny lydfil", "badge-warn"),
    STATE_DESIGN_CHANGED: ("Nytt utseende", "badge-warn"),
    STATE_FAILED: ("Feilet", "badge-error"),
}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def clean_folder_name(name: str) -> str:
    """A single Canvas folder name: no slashes or control characters."""
    cleaned = _UNSAFE_FOLDER_CHARS.sub("-", name or "")
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:MAX_FOLDER_NAME].strip(" .")


def load_design(session: Session) -> PlayerDesign:
    return PlayerDesign(
        style=get_value(session, "canvas_player_style"),
        color=get_value(session, "canvas_player_color"),
        label=get_value(session, "canvas_player_label"),
        icon=get_value(session, "canvas_player_icon"),
    ).normalized()


def load_folder(session: Session) -> str:
    return clean_folder_name(get_value(session, "canvas_audio_folder")) or DEFAULT_FOLDER


def save_design(session: Session, design: PlayerDesign, folder: str) -> PlayerDesign:
    normalized = design.normalized()
    set_value(session, "canvas_player_style", normalized.style)
    set_value(session, "canvas_player_color", normalized.color)
    set_value(session, "canvas_player_label", normalized.label)
    set_value(session, "canvas_player_icon", normalized.icon)
    set_value(session, "canvas_audio_folder", clean_folder_name(folder) or DEFAULT_FOLDER)
    return normalized


def module_folder(root: str, module: Module | None) -> str:
    root = clean_folder_name(root) or DEFAULT_FOLDER
    if module is None:
        return f"{root}/00 Uten modul"
    return f"{root}/{module.position:02d} {clean_folder_name(module.name) or 'Modul'}"


# --------------------------------------------------------------------------- #
# State shown in the GUI
# --------------------------------------------------------------------------- #


def page_state(page: Page, design_fingerprint: str) -> str:
    embed = page.canvas_embed
    if page.audio is None or not audio_is_current(page):
        return STATE_NO_AUDIO
    if embed is None:
        return STATE_NOT_PUBLISHED
    if embed.last_error:
        return STATE_FAILED
    if not embed.embedded:
        return STATE_NOT_PUBLISHED
    if embed.file_hash != page.audio.file_hash:
        return STATE_FILE_CHANGED
    if embed.design_fingerprint != design_fingerprint:
        return STATE_DESIGN_CHANGED
    return STATE_PUBLISHED


def module_rows(session: Session, course: Course) -> list[dict[str, Any]]:
    fingerprint = load_design(session).fingerprint
    rows: list[dict[str, Any]] = []
    modules = session.execute(
        select(Module).where(Module.course_id == course.id).order_by(Module.position)
    ).scalars()
    for module in modules:
        spoken = [p for p in module.pages if not p.archived and p.tts_text.strip()]
        states = [page_state(p, fingerprint) for p in spoken]
        rows.append(
            {
                "id": module.id,
                "name": module.name,
                "pages": len(spoken),
                "ready": sum(1 for s in states if s != STATE_NO_AUDIO),
                "published": states.count(STATE_PUBLISHED),
                "outdated": states.count(STATE_FILE_CHANGED) + states.count(STATE_DESIGN_CHANGED),
                "failed": states.count(STATE_FAILED),
                "on_canvas": sum(
                    1 for p in module.pages if p.canvas_embed and p.canvas_embed.embedded
                ),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PageTask:
    page_id: int
    title: str
    page_url: str
    canvas_course_id: str
    relative_path: str = ""
    file_hash: str = ""
    file_name: str = ""
    folder_path: str = ""
    file_id: str = ""
    embed_file_hash: str = ""
    embed_design: str = ""
    embedded: bool = False


@dataclass(slots=True)
class PageOutcome:
    page_id: int
    title: str
    status: str  # published | unchanged | removed | skipped | failed
    message: str = ""


@dataclass(slots=True)
class Plan:
    course_name: str
    design: PlayerDesign
    tasks: list[PageTask] = field(default_factory=list)
    skipped: list[PageOutcome] = field(default_factory=list)


def prepare(session: Session, action: str, module_ids: list[int] | None) -> Plan:
    """Decide which pages to touch. ``module_ids=None`` means the whole course."""
    course = selected_course(session)
    if course is None:
        raise ConfigurationError("Velg og synkroniser et Canvas-emne først.", provider="canvas")

    query = select(Page).where(Page.course_id == course.id, Page.archived.is_(False))
    if module_ids is not None:
        query = query.where(Page.module_id.in_(module_ids))
    pages = sorted(
        session.execute(query).scalars().all(),
        key=lambda p: (p.module.position if p.module else 0, p.position, p.title),
    )

    plan = Plan(course_name=course.name, design=load_design(session))
    root = load_folder(session)
    for page in pages:
        embed = page.canvas_embed
        base = {
            "page_id": page.id,
            "title": page.title,
            "page_url": page.canvas_url,
            "canvas_course_id": course.canvas_course_id,
        }
        if action == "remove":
            if embed is not None and embed.embedded:
                plan.tasks.append(PageTask(**base, file_id=embed.file_id))
            continue

        if not page.tts_text.strip():
            continue
        if page.audio is None or not audio_is_current(page):
            plan.skipped.append(
                PageOutcome(
                    page.id,
                    page.title,
                    "skipped",
                    "Lyden er ikke generert, eller teksten er endret siden.",
                )
            )
            continue
        plan.tasks.append(
            PageTask(
                **base,
                relative_path=page.audio.relative_path,
                file_hash=page.audio.file_hash,
                file_name=PurePosixPath(page.audio.relative_path).name,
                folder_path=module_folder(root, page.module),
                file_id=embed.file_id if embed else "",
                embed_file_hash=embed.file_hash if embed else "",
                embed_design=embed.design_fingerprint if embed else "",
                embedded=bool(embed and embed.embedded),
            )
        )
    return plan


# --------------------------------------------------------------------------- #
# Recording results
# --------------------------------------------------------------------------- #


def _embed(session: Session, page_id: int) -> CanvasEmbed:
    embed = session.scalar(select(CanvasEmbed).where(CanvasEmbed.page_id == page_id))
    if embed is None:
        embed = CanvasEmbed(page_id=page_id)
        session.add(embed)
    return embed


def _refresh_local_page(session: Session, page_id: int, body: CanvasPageBody | None) -> None:
    page = session.get(Page, page_id)
    if page is not None and body is not None:
        refresh_page_body(session, page, body)


def record_published(
    session: Session,
    task: PageTask,
    *,
    file_id: str,
    design_fingerprint: str,
    body: CanvasPageBody | None,
) -> None:
    embed = _embed(session, task.page_id)
    embed.file_id = file_id
    embed.file_hash = task.file_hash
    embed.folder_path = task.folder_path
    embed.design_fingerprint = design_fingerprint
    embed.embedded = True
    embed.last_error = ""
    _refresh_local_page(session, task.page_id, body)
    session.flush()


def record_removed(session: Session, task: PageTask, body: CanvasPageBody | None) -> None:
    embed = _embed(session, task.page_id)
    embed.embedded = False
    embed.last_error = ""
    _refresh_local_page(session, task.page_id, body)
    session.flush()


def record_error(session: Session, page_id: int, message: str) -> None:
    _embed(session, page_id).last_error = message
    session.flush()


# --------------------------------------------------------------------------- #
# Doing the work
# --------------------------------------------------------------------------- #

MARKER_LOST = (
    "Canvas lagret siden, men fjernet merkingen av avspilleren. Den kan derfor ikke "
    "byttes ut eller fjernes automatisk senere."
)


async def publish_page(client: CanvasClient, task: PageTask, design: PlayerDesign) -> PageOutcome:
    fingerprint = design.fingerprint
    file_id = task.file_id if task.embed_file_hash == task.file_hash else ""
    if file_id and await client.get_file(file_id) is None:
        file_id = ""  # deleted in Canvas since the last run

    uploaded = False
    if not file_id:
        path = resolve_path(task.relative_path)
        if not path.is_file():
            raise ValueError("Lydfilen finnes ikke på disk. Generer den på nytt.")
        canvas_file = await client.upload_file(
            task.canvas_course_id, path=path, name=task.file_name, folder_path=task.folder_path
        )
        file_id, uploaded = canvas_file.id, True

    page = await client.get_page(task.canvas_course_id, task.page_url)
    if (
        not uploaded
        and task.embedded
        and file_id == task.file_id
        and task.embed_design == fingerprint
        and has_player(page.body)
    ):
        await run_db(
            lambda session: record_published(
                session, task, file_id=file_id, design_fingerprint=fingerprint, body=None
            )
        )
        return PageOutcome(
            task.page_id, task.title, "unchanged", "Avspilleren er allerede oppdatert."
        )

    block = player_html(design, player_src(task.canvas_course_id, file_id))
    updated = await client.update_page_body(
        task.canvas_course_id, task.page_url, with_player(page.body, block)
    )
    await run_db(
        lambda session: record_published(
            session, task, file_id=file_id, design_fingerprint=fingerprint, body=updated
        )
    )
    if not has_player(updated.body):
        return PageOutcome(task.page_id, task.title, "published", MARKER_LOST)
    message = (
        "Lydfilen er lastet opp og avspilleren lagt inn."
        if uploaded
        else "Avspilleren er oppdatert."
    )
    return PageOutcome(task.page_id, task.title, "published", message)


async def remove_page(client: CanvasClient, task: PageTask) -> PageOutcome:
    page = await client.get_page(task.canvas_course_id, task.page_url)
    updated: CanvasPageBody | None = None
    if has_player(page.body):
        updated = await client.update_page_body(
            task.canvas_course_id, task.page_url, strip_players(page.body)
        )
    await run_db(lambda session: record_removed(session, task, updated))
    return PageOutcome(
        task.page_id,
        task.title,
        "removed",
        "Avspilleren er fjernet. Lydfilen ligger fortsatt i Canvas.",
    )


# --------------------------------------------------------------------------- #
# Runs in the background
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PublishRun:
    id: int
    action: str
    course_name: str
    total: int
    done: int = 0
    current: str = ""
    outcomes: list[PageOutcome] = field(default_factory=list)
    error: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def counts(self) -> dict[str, int]:
        counts = {"published": 0, "unchanged": 0, "removed": 0, "skipped": 0, "failed": 0}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return counts

    def summary(self) -> str:
        if self.error:
            return self.error
        c = self.counts()
        if self.action == "remove":
            return f"{c['removed']} avspillere fjernet, {c['failed']} feilet."
        return (
            f"{c['published']} publisert, {c['unchanged']} uendret, "
            f"{c['skipped']} hoppet over, {c['failed']} feilet."
        )


_current: PublishRun | None = None
_task: asyncio.Task[PublishRun] | None = None
_run_counter = 0


def current_run() -> PublishRun | None:
    return _current


def new_run(action: str, plan: Plan) -> PublishRun:
    global _current, _run_counter
    if _current is not None and _current.running:
        raise RuntimeError("En publisering til Canvas pågår allerede.")
    _run_counter += 1
    _current = PublishRun(
        id=_run_counter,
        action=action,
        course_name=plan.course_name,
        total=len(plan.tasks) + len(plan.skipped),
    )
    _current.outcomes.extend(plan.skipped)
    _current.done = len(plan.skipped)
    return _current


async def execute(
    run: PublishRun, plan: Plan, *, transport: httpx.AsyncBaseTransport | None = None
) -> PublishRun:
    """Work through the plan. Never raises: problems end up in the run."""
    settings = get_settings()
    try:
        async with CanvasClient(
            settings.canvas_url, settings.canvas_token, transport=transport
        ) as client:
            for task in plan.tasks:
                run.current = task.title
                try:
                    if run.action == "remove":
                        outcome = await remove_page(client, task)
                    else:
                        outcome = await publish_page(client, task, plan.design)
                except (ServiceError, ValueError, OSError) as exc:
                    message = getattr(exc, "message", None) or str(exc)
                    logger.warning(
                        "Canvas %s failed for page %s: %s", run.action, task.page_id, message
                    )
                    await run_db(
                        lambda session, t=task, m=message: record_error(session, t.page_id, m)
                    )
                    outcome = PageOutcome(task.page_id, task.title, "failed", message)
                run.outcomes.append(outcome)
                run.done += 1
    except ServiceError as exc:
        run.error = exc.message
    except Exception as exc:  # a run must never be left hanging
        logger.exception("Canvas publish run crashed")
        run.error = f"Uventet feil: {exc}"
    finally:
        run.current = ""
        run.finished_at = datetime.now(UTC)
    logger.info("Canvas %s finished: %s", run.action, run.summary())
    return run


def launch(run: PublishRun, plan: Plan) -> None:
    """Start the run on the event loop and keep a reference so it is not collected."""
    global _task
    _task = asyncio.get_running_loop().create_task(execute(run, plan))


def reset_runs() -> None:
    """Forget the current run. Used by tests."""
    global _current, _task
    _current = None
    _task = None
