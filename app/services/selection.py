"""Resolving which pages a request is about.

The GUI offers four shortcuts — generate missing, generate changed, regenerate
selected, regenerate everything — and both the estimate endpoint and the job
endpoint must interpret them identically.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import Page, PageAudioStatus
from app.services.hashing import page_audio_status
from app.services.presenters import selected_course

CHANGED_STATUSES = {PageAudioStatus.CANVAS_CHANGED, PageAudioStatus.TEXT_CHANGED}


def course_pages(session: Session, course_id: int) -> list[Page]:
    return list(
        session.execute(
            select(Page)
            .where(Page.course_id == course_id, Page.archived.is_(False))
            .order_by(Page.module_id, Page.position, Page.title)
        )
        .scalars()
        .all()
    )


def resolve_pages(
    session: Session,
    *,
    mode: str,
    page_ids: list[int] | None = None,
    course_id: int | None = None,
) -> list[Page]:
    """Return the pages a request targets, in Canvas order."""
    if course_id is None:
        course = selected_course(session)
        course_id = course.id if course else None

    if mode == "selected":
        if not page_ids:
            return []
        pages = list(
            session.execute(select(Page).where(Page.id.in_(page_ids), Page.archived.is_(False)))
            .scalars()
            .all()
        )
        order = {page_id: index for index, page_id in enumerate(page_ids)}
        pages.sort(key=lambda p: order.get(p.id, 10**6))
        return pages

    if course_id is None:
        return []

    pages = course_pages(session, course_id)
    if mode == "all":
        return pages
    if mode == "missing":
        return [p for p in pages if page_audio_status(p) == PageAudioStatus.NOT_GENERATED]
    if mode == "changed":
        return [p for p in pages if page_audio_status(p) in CHANGED_STATUSES]
    return []
