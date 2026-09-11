"""Synchronising a Canvas course into the local database.

The rule that keeps the bill down: **a sync never generates or regenerates
audio.** It updates content and hashes; the derived status then tells the user
which pages *would* need regenerating, and they decide.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import run_db
from app.logging_config import get_logger
from app.models.core import Course, Module, Page
from app.services.canvas_service import (
    CanvasClient,
    CanvasModule,
    CanvasPageBody,
    CanvasPageSummary,
)
from app.services.hashing import text_hash
from app.services.naming import course_dirname, module_dirname
from app.services.text_service import html_to_text

logger = get_logger("app.services.sync")

#: Canvas is rate limited; a handful of parallel body fetches is plenty.
BODY_FETCH_CONCURRENCY = 4


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo, so stored timestamps come back naive.

    Without this, every re-sync would look like a Canvas change and re-fetch
    every page body.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _same_moment(stored: datetime | None, incoming: datetime | None) -> bool:
    left, right = _as_utc(stored), _as_utc(incoming)
    if left is None or right is None:
        return left is None and right is None
    return abs((left - right).total_seconds()) < 1


@dataclass(slots=True)
class SyncResult:
    course_id: int
    course_name: str
    canvas_course_id: str
    modules: int = 0
    pages: int = 0
    new_pages: int = 0
    changed_pages: int = 0
    unchanged_pages: int = 0
    archived_pages: int = 0
    errors: list[str] = field(default_factory=list)

    def as_message(self) -> str:
        parts = [
            f"{self.modules} moduler",
            f"{self.pages} sider",
            f"{self.new_pages} nye",
            f"{self.changed_pages} endret",
        ]
        if self.archived_pages:
            parts.append(f"{self.archived_pages} fjernet fra Canvas")
        return ", ".join(parts)


def _upsert_structure(
    session: Session,
    *,
    canvas_course_id: str,
    course_name: str,
    course_code: str,
    modules: list[CanvasModule],
    pages: list[CanvasPageSummary],
) -> tuple[int, list[tuple[int, str, datetime | None]], int, int]:
    """Create/update course, modules and page rows.

    Returns ``(course_pk, pages_needing_body, archived_count, new_page_count)``
    where each entry of ``pages_needing_body`` is
    ``(page_pk, canvas_page_url, canvas_updated_at)``.
    """
    course = session.scalar(select(Course).where(Course.canvas_course_id == canvas_course_id))
    if course is None:
        course = Course(canvas_course_id=canvas_course_id, name=course_name)
        session.add(course)
    course.name = course_name
    course.course_code = course_code
    course.slug = course_dirname(course_name)
    course.is_selected = True
    course.synced_at = datetime.now(UTC)
    session.flush()

    # Any other course loses "selected".
    for other in session.execute(select(Course).where(Course.id != course.id)).scalars():
        other.is_selected = False

    # -- modules ---------------------------------------------------------
    existing_modules = {
        m.canvas_module_id: m
        for m in session.execute(select(Module).where(Module.course_id == course.id)).scalars()
    }
    seen_module_ids: set[str] = set()
    module_by_canvas_id: dict[str, Module] = {}

    for position, canvas_module in enumerate(modules, start=1):
        module = existing_modules.get(canvas_module.id)
        if module is None:
            module = Module(course_id=course.id, canvas_module_id=canvas_module.id)
            session.add(module)
        module.name = canvas_module.name
        module.position = canvas_module.position or position
        module.slug = module_dirname(canvas_module.name, module.position)
        seen_module_ids.add(canvas_module.id)
        module_by_canvas_id[canvas_module.id] = module
    session.flush()

    for canvas_id, module in existing_modules.items():
        if canvas_id not in seen_module_ids:
            session.delete(module)
    session.flush()

    # -- page → module mapping from module items -------------------------
    placement: dict[str, tuple[Module | None, int]] = {}
    for canvas_module in modules:
        module = module_by_canvas_id.get(canvas_module.id)
        order = 0
        for item in sorted(canvas_module.items, key=lambda i: i.position):
            if not item.is_page:
                continue
            order += 1
            placement[item.page_url] = (module, order)

    # -- pages -----------------------------------------------------------
    existing_pages = {
        p.canvas_page_id: p
        for p in session.execute(select(Page).where(Page.course_id == course.id)).scalars()
    }
    seen_page_ids: set[str] = set()
    needs_body: list[tuple[int, str, datetime | None]] = []
    new_pages = 0
    loose_position = 0

    for summary in pages:
        module, position = placement.get(summary.url, (None, 0))
        if module is None:
            loose_position += 1
            position = loose_position

        page = existing_pages.get(summary.page_id)
        is_new = page is None
        if is_new:
            new_pages += 1
        if page is None:
            page = Page(
                course_id=course.id,
                canvas_page_id=summary.page_id,
                title=summary.title,
            )
            session.add(page)

        page.title = summary.title
        page.canvas_url = summary.url
        page.module_id = module.id if module is not None else None
        page.position = position
        page.archived = False
        seen_page_ids.add(summary.page_id)
        session.flush()

        timestamp_changed = summary.updated_at is not None and not _same_moment(
            page.canvas_updated_at, summary.updated_at
        )
        if is_new or timestamp_changed or not page.html:
            needs_body.append((page.id, summary.url, summary.updated_at))

    archived = 0
    for canvas_page_id, page in existing_pages.items():
        if canvas_page_id not in seen_page_ids and not page.archived:
            page.archived = True
            archived += 1

    session.flush()
    return course.id, needs_body, archived, new_pages


def _store_body(session: Session, page_pk: int, body: CanvasPageBody) -> bool:
    """Persist a fetched page body. Returns True when the text actually changed."""
    page = session.get(Page, page_pk)
    if page is None:
        return False

    text = html_to_text(body.body)
    new_hash = text_hash(text)
    changed = new_hash != page.original_hash

    page.html = body.body
    page.original_text = text
    page.original_hash = new_hash
    page.title = body.title or page.title
    page.canvas_updated_at = body.updated_at
    page.fetched_at = datetime.now(UTC)
    page.last_error = ""
    session.flush()
    return changed


async def sync_course(
    *,
    base_url: str,
    token: str,
    canvas_course_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SyncResult:
    """Fetch a whole course from Canvas and merge it into the database."""
    async with CanvasClient(base_url, token, transport=transport) as client:
        course = await client.get_course(canvas_course_id)
        modules = await client.list_modules(canvas_course_id)
        pages = await client.list_pages(canvas_course_id)

        course_pk, needs_body, archived, new_pages = await run_db(
            lambda session: _upsert_structure(
                session,
                canvas_course_id=course.id,
                course_name=course.name,
                course_code=course.course_code,
                modules=modules,
                pages=pages,
            )
        )

        result = SyncResult(
            course_id=course_pk,
            course_name=course.name,
            canvas_course_id=course.id,
            modules=len(modules),
            pages=len(pages),
            new_pages=new_pages,
            archived_pages=archived,
        )

        semaphore = asyncio.Semaphore(BODY_FETCH_CONCURRENCY)
        total = len(needs_body)

        async def fetch_one(index: int, page_pk: int, page_url: str) -> None:
            async with semaphore:
                logger.info("Processing page %d/%d url=%s", index, total, page_url)
                try:
                    body = await client.get_page(canvas_course_id, page_url)
                except Exception as exc:  # one bad page must not fail the sync
                    detail = str(exc)
                    result.errors.append(f"{page_url}: {detail}")
                    logger.warning("Failed to fetch page %s: %s", page_url, detail)
                    await run_db(lambda session: _record_page_error(session, page_pk, detail))
                    return
                changed = await run_db(lambda session: _store_body(session, page_pk, body))
                if changed:
                    result.changed_pages += 1

        await asyncio.gather(
            *(
                fetch_one(index, page_pk, page_url)
                for index, (page_pk, page_url, _updated) in enumerate(needs_body, start=1)
            )
        )

    # Pages that were not refetched still get the current cleaner applied.
    refetched = {page_pk for page_pk, _url, _updated in needs_body}
    result.changed_pages += await run_db(
        lambda session: _reclean_stored_pages(session, course_pk, refetched)
    )
    result.unchanged_pages = max(result.pages - result.changed_pages, 0)
    logger.info("Canvas sync finished %s", result.as_message())
    return result


def _record_page_error(session: Session, page_pk: int, message: str) -> None:
    page = session.get(Page, page_pk)
    if page is not None:
        page.last_error = message[:1000]


def _reclean_stored_pages(session: Session, course_pk: int, skip_ids: set[int]) -> int:
    """Re-run the cleaner on stored HTML for pages that were not refetched.

    Keeps stored text in step with improvements to the cleaner, such as new
    punctuation rules, without another Canvas call. Returns how many pages'
    text actually changed.
    """
    changed = 0
    pages = session.execute(
        select(Page).where(Page.course_id == course_pk, Page.archived.is_(False))
    ).scalars()
    for page in pages:
        if page.id in skip_ids or not page.html:
            continue
        text = html_to_text(page.html)
        if text != page.original_text:
            page.original_text = text
            page.original_hash = text_hash(text)
            changed += 1
    session.flush()
    return changed


def refresh_page_body(session: Session, page: Page, body: CanvasPageBody) -> bool:
    """Public wrapper used when refetching a single page from the page detail view."""
    return _store_body(session, page.id, body)


def course_structure(session: Session, course_id: int) -> dict[str, Any]:
    """Modules with their pages, in Canvas order, for the Canvas view."""
    course = session.get(Course, course_id)
    if course is None:
        return {"course": None, "groups": []}

    modules = (
        session.execute(
            select(Module).where(Module.course_id == course_id).order_by(Module.position)
        )
        .scalars()
        .all()
    )
    groups: list[dict[str, Any]] = []
    for module in modules:
        module_pages = [p for p in module.pages if not p.archived]
        module_pages.sort(key=lambda p: (p.position, p.title))
        groups.append({"module": module, "pages": module_pages})

    loose = (
        session.execute(
            select(Page)
            .where(Page.course_id == course_id, Page.module_id.is_(None), Page.archived.is_(False))
            .order_by(Page.position, Page.title)
        )
        .scalars()
        .all()
    )
    if loose:
        groups.append({"module": None, "pages": loose})

    archived = (
        session.execute(
            select(Page)
            .where(Page.course_id == course_id, Page.archived.is_(True))
            .order_by(Page.title)
        )
        .scalars()
        .all()
    )
    return {"course": course, "groups": groups, "archived": archived}
