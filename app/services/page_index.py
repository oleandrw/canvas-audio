"""The page list behind the Canvas view, read cheaply.

A course can hold several hundred pages, and every one of them carries the raw
Canvas HTML. Rendering a list of titles must not drag all of that out of the
database, so this module selects the columns it actually needs — never
``Page.html`` — and derives the status once per page instead of once per place
that shows it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.core import GeneratedAudio, Module, Page, PageAudioStatus
from app.services.audio_service import format_bytes, format_duration
from app.services.hashing import audio_status, status_label

#: The statuses the GUI can filter on, and what each one means.
FILTERS: dict[str, tuple[str, set[str]]] = {
    "alle": ("Alle sider", set()),
    "ikke_generert": ("Ikke generert", {PageAudioStatus.NOT_GENERATED}),
    "endret": (
        "Endret siden sist",
        {PageAudioStatus.CANVAS_CHANGED, PageAudioStatus.TEXT_CHANGED},
    ),
    "oppdatert": ("Oppdatert", {PageAudioStatus.UP_TO_DATE}),
    "feilet": ("Feilet", {PageAudioStatus.FAILED}),
}

#: How many rows a search may return before the user is asked to be precise.
RESULT_CAP = 200

#: "Uten modul" has no id of its own, so the GUI addresses it as 0.
NO_MODULE = 0


@dataclass(slots=True)
class PageRow:
    id: int
    module_id: int
    module_name: str
    title: str
    characters: int
    words: int
    status: str
    status_label: str


@dataclass(slots=True)
class ModuleSummary:
    id: int
    name: str
    pages: int = 0
    ready: int = 0
    missing: int = 0
    changed: int = 0
    failed: int = 0


def _query(course_id: int) -> Select:
    """Everything the list needs, and deliberately not ``Page.html``."""
    return (
        select(
            Page.id,
            Page.module_id,
            Page.title,
            Page.position,
            Page.last_error,
            Page.original_hash,
            Page.original_text,
            Page.processed_text,
            GeneratedAudio.id.label("audio_id"),
            GeneratedAudio.source_text_hash,
            GeneratedAudio.source_canvas_hash,
            Module.name.label("module_name"),
            Module.position.label("module_position"),
        )
        .outerjoin(GeneratedAudio, GeneratedAudio.page_id == Page.id)
        .outerjoin(Module, Module.id == Page.module_id)
        .where(Page.course_id == course_id, Page.archived.is_(False))
        # Pages without a module come last, the way the Canvas view has always
        # shown them; SQLite would otherwise sort their NULL position first.
        .order_by(Module.position.is_(None), Module.position, Page.position, Page.title)
    )


def _row(record: object) -> PageRow:
    tts_text = record.processed_text or record.original_text
    status = audio_status(
        has_audio=record.audio_id is not None,
        audio_text_hash=record.source_text_hash or "",
        audio_canvas_hash=record.source_canvas_hash or "",
        tts_text=tts_text,
        page_hash=record.original_hash,
        last_error=record.last_error,
    )
    return PageRow(
        id=record.id,
        module_id=record.module_id or NO_MODULE,
        module_name=record.module_name or "Uten modul",
        title=record.title,
        characters=len(tts_text),
        words=len(tts_text.split()),
        status=status,
        status_label=status_label(status)[1],
    )


def course_rows(
    session: Session,
    course_id: int,
    *,
    search: str = "",
    status: str = "alle",
    module_id: int | None = None,
) -> list[PageRow]:
    """The pages of a course, narrowed by the GUI's search and filter."""
    query = _query(course_id)
    if module_id is not None:
        if module_id == NO_MODULE:
            query = query.where(Page.module_id.is_(None))
        else:
            query = query.where(Page.module_id == module_id)
    if search := search.strip():
        like = f"%{search}%"
        query = query.where(or_(Page.title.ilike(like), Module.name.ilike(like)))

    rows = [_row(record) for record in session.execute(query)]
    wanted = FILTERS.get(status, FILTERS["alle"])[1]
    return [row for row in rows if not wanted or row.status in wanted]


def module_summaries(session: Session, course_id: int) -> list[ModuleSummary]:
    """One line per module, counted from a single pass over the pages.

    Empty modules are listed too: they exist in Canvas, so hiding them would
    look like the synchronisation lost something.
    """
    modules = session.execute(
        select(Module.id, Module.name)
        .where(Module.course_id == course_id)
        .order_by(Module.position)
    ).all()
    order = {module.id: ModuleSummary(id=module.id, name=module.name) for module in modules}
    loose = ModuleSummary(id=NO_MODULE, name="Uten modul")
    for record in session.execute(_query(course_id)):
        row = _row(record)
        summary = order.get(row.module_id, loose)
        summary.pages += 1
        if row.status == PageAudioStatus.UP_TO_DATE:
            summary.ready += 1
        elif row.status == PageAudioStatus.NOT_GENERATED:
            summary.missing += 1
        elif row.status == PageAudioStatus.FAILED:
            summary.failed += 1
        else:
            summary.changed += 1
    summaries = list(order.values())
    if loose.pages:
        summaries.append(loose)
    return summaries


def course_stats(session: Session, course_id: int) -> dict[str, object]:
    """The numbers on the dashboard and above the page list, hashed once."""
    summaries = module_summaries(session, course_id)
    modules = sum(1 for summary in summaries if summary.id != NO_MODULE)
    totals = {"pages": 0, "ready": 0, "missing": 0, "changed": 0, "failed": 0}
    for summary in summaries:
        for key in totals:
            totals[key] += getattr(summary, key)

    seconds, size = session.execute(
        select(
            func.coalesce(func.sum(GeneratedAudio.duration_seconds), 0.0),
            func.coalesce(func.sum(GeneratedAudio.size_bytes), 0),
        )
        .join(Page, Page.id == GeneratedAudio.page_id)
        .where(Page.course_id == course_id, Page.archived.is_(False))
    ).one()

    return {
        "modules": modules,
        "pages": totals["pages"],
        "generated": totals["pages"] - totals["missing"] - totals["failed"],
        "not_generated": totals["missing"],
        "changed": totals["changed"],
        "failed": totals["failed"],
        # Unchanged from before: a failed page is counted on its own, not here.
        "remaining": totals["missing"] + totals["changed"],
        "total_duration_label": format_duration(float(seconds or 0.0)),
        "total_size_label": format_bytes(int(size or 0)),
    }


def archived_count(session: Session, course_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(Page.id)).where(Page.course_id == course_id, Page.archived.is_(True))
        )
        or 0
    )
