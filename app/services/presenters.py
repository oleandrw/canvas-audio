"""Turn ORM rows into the shapes the API and the templates consume.

Keeping this in one place means the JSON API and the HTML views can never drift
apart, and the Norwegian labels live next to the values they describe.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.core import (
    Course,
    GenerationJob,
    ItemStatus,
    JobStatus,
    Module,
    Page,
    UsageRecord,
)
from app.pricing import load_pricing
from app.schemas.canvas import CourseOut, ModuleOut, PageSummaryOut
from app.schemas.costs import ProviderUsage, UsageSummary
from app.schemas.generation import JobItemOut, JobOut
from app.schemas.pages import AudioInfo, PageDetail, SoundCloudInfo
from app.services.audio_service import audio_exists, format_duration
from app.services.hashing import page_audio_status, status_label, text_hash

ITEM_STATUS_LABELS: dict[str, str] = {
    ItemStatus.PENDING: "Venter",
    ItemStatus.CLEANING: "Renser tekst",
    ItemStatus.AI_PROCESSING: "AI-behandling",
    ItemStatus.READY_FOR_TTS: "Klar for lyd",
    ItemStatus.GENERATING_AUDIO: "Genererer lyd",
    ItemStatus.DONE: "Ferdig",
    ItemStatus.SKIPPED: "Hoppet over",
    ItemStatus.FAILED: "Feilet",
}

JOB_STATUS_LABELS: dict[str, str] = {
    JobStatus.QUEUED: "I kø",
    JobStatus.RUNNING: "Kjører",
    JobStatus.COMPLETED: "Ferdig",
    JobStatus.COMPLETED_WITH_ERRORS: "Ferdig med feil",
    JobStatus.FAILED: "Feilet",
    JobStatus.CANCELLED: "Avbrutt",
}

PROVIDER_LABELS: dict[str, str] = {
    "none": "Ingen AI",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "elevenlabs": "ElevenLabs",
}


def item_status_label(status: str) -> str:
    return ITEM_STATUS_LABELS.get(status, status)


def job_status_label(status: str) -> str:
    return JOB_STATUS_LABELS.get(status, status)


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider or "Ingen AI")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


def page_summary(page: Page) -> PageSummaryOut:
    status = page_audio_status(page)
    _, label, _ = status_label(status)
    return PageSummaryOut(
        id=page.id,
        title=page.title,
        position=page.position,
        characters=page.character_count,
        words=page.word_count,
        status=status,
        status_label=label,
        module_id=page.module_id,
        module_name=page.module.name if page.module else "Uten modul",
        has_audio=page.audio is not None,
        archived=page.archived,
        canvas_updated_at=page.canvas_updated_at,
        soundcloud_track_id=page.soundcloud_track.track_id if page.soundcloud_track else "",
    )


def page_detail(page: Page) -> PageDetail:
    status = page_audio_status(page)
    _, label, _ = status_label(status)

    audio_info: AudioInfo | None = None
    if page.audio is not None:
        audio_info = AudioInfo(
            relative_path=page.audio.relative_path,
            size_bytes=page.audio.size_bytes,
            duration_seconds=page.audio.duration_seconds,
            duration_label=format_duration(page.audio.duration_seconds),
            voice_name=page.audio.voice_name,
            voice_id=page.audio.voice_id,
            model_id=page.audio.model_id,
            character_count=page.audio.character_count,
            generated_at=page.audio.generated_at,
            exists_on_disk=audio_exists(page.audio.relative_path),
        )

    sc_info: SoundCloudInfo | None = None
    track = page.soundcloud_track
    if track is not None:
        sc_info = SoundCloudInfo(
            track_id=track.track_id,
            permalink_url=track.permalink_url,
            uploaded_at=track.uploaded_at,
            up_to_date=bool(page.audio and track.file_hash == page.audio.file_hash),
        )

    return PageDetail(
        id=page.id,
        title=page.title,
        canvas_page_id=page.canvas_page_id,
        canvas_url=page.canvas_url,
        module_id=page.module_id,
        module_name=page.module.name if page.module else "Uten modul",
        position=page.position,
        canvas_updated_at=page.canvas_updated_at,
        archived=page.archived,
        original_text=page.original_text,
        processed_text=page.processed_text,
        processed_by=page.processed_by,
        processed_model=page.processed_model,
        manually_edited=page.manually_edited,
        characters=page.character_count,
        words=page.word_count,
        status=status,
        status_label=label,
        last_error=page.last_error,
        audio=audio_info,
        soundcloud=sc_info,
    )


# --------------------------------------------------------------------------- #
# Courses
# --------------------------------------------------------------------------- #


def course_out(session: Session, course: Course, *, include_pages: bool = True) -> CourseOut:
    modules = (
        session.execute(
            select(Module).where(Module.course_id == course.id).order_by(Module.position)
        )
        .scalars()
        .all()
    )
    module_outs: list[ModuleOut] = []
    page_total = 0

    if include_pages:
        for module in modules:
            pages = sorted(
                (p for p in module.pages if not p.archived), key=lambda p: (p.position, p.title)
            )
            page_total += len(pages)
            module_outs.append(
                ModuleOut(
                    id=module.id,
                    name=module.name,
                    position=module.position,
                    pages=[page_summary(p) for p in pages],
                )
            )
        loose = (
            session.execute(
                select(Page)
                .where(
                    Page.course_id == course.id,
                    Page.module_id.is_(None),
                    Page.archived.is_(False),
                )
                .order_by(Page.position, Page.title)
            )
            .scalars()
            .all()
        )
        if loose:
            page_total += len(loose)
            module_outs.append(
                ModuleOut(
                    id=None,
                    name="Uten modul",
                    position=999,
                    pages=[page_summary(p) for p in loose],
                )
            )
    else:
        page_total = int(
            session.scalar(
                select(func.count(Page.id)).where(
                    Page.course_id == course.id, Page.archived.is_(False)
                )
            )
            or 0
        )

    return CourseOut(
        id=course.id,
        canvas_course_id=course.canvas_course_id,
        name=course.name,
        course_code=course.course_code,
        synced_at=course.synced_at,
        module_count=len(modules),
        page_count=page_total,
        modules=module_outs,
    )


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


def job_out(job: GenerationJob, *, include_items: bool = True) -> JobOut:
    processed = job.processed_items
    percent = int(processed / job.total_items * 100) if job.total_items else 0
    items: list[JobItemOut] = []
    if include_items:
        for item in job.items:
            page = item.page
            items.append(
                JobItemOut(
                    id=item.id,
                    page_id=item.page_id,
                    page_title=page.title if page else f"Side {item.page_id}",
                    module_name=(page.module.name if page and page.module else "Uten modul"),
                    status=item.status,
                    status_label=item_status_label(item.status),
                    message=item.message,
                    characters=item.characters,
                    finished_at=item.finished_at,
                )
            )
    return JobOut(
        id=job.id,
        status=job.status,
        status_label=job_status_label(job.status),
        ai_provider=job.ai_provider,
        ai_model=job.ai_model,
        voice_name=job.voice_name,
        tts_model=job.tts_model,
        total_items=job.total_items,
        completed_items=job.completed_items,
        failed_items=job.failed_items,
        skipped_items=job.skipped_items,
        processed_items=processed,
        progress_percent=percent,
        estimated_cost_usd=job.estimated_cost_usd,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        items=items,
    )


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #


def month_start(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage_summary(
    session: Session,
    *,
    scope: str,
    since: datetime | None = None,
    job_id: int | None = None,
) -> UsageSummary:
    pricing = load_pricing()
    query = select(
        UsageRecord.provider,
        func.sum(UsageRecord.characters),
        func.sum(UsageRecord.input_tokens),
        func.sum(UsageRecord.output_tokens),
        func.sum(func.coalesce(UsageRecord.actual_cost_usd, UsageRecord.estimated_cost_usd)),
        func.count(UsageRecord.id),
    ).group_by(UsageRecord.provider)

    if since is not None:
        query = query.where(UsageRecord.created_at >= since)
    if job_id is not None:
        query = query.where(UsageRecord.job_id == job_id)

    providers: list[ProviderUsage] = []
    total = 0.0
    for provider, characters, inp, out, cost, calls in session.execute(query).all():
        cost_value = float(cost or 0.0)
        total += cost_value
        providers.append(
            ProviderUsage(
                provider=provider,
                characters=int(characters or 0),
                input_tokens=int(inp or 0),
                output_tokens=int(out or 0),
                cost_usd=cost_value,
                calls=int(calls or 0),
            )
        )
    providers.sort(key=lambda p: p.cost_usd, reverse=True)

    return UsageSummary(
        scope=scope,
        total_usd=total,
        total_local=pricing.to_local(total),
        currency_code=pricing.currency.code,
        currency_symbol=pricing.currency.symbol,
        providers=providers,
    )


def spent_this_month(session: Session) -> float:
    value = session.scalar(
        select(
            func.sum(func.coalesce(UsageRecord.actual_cost_usd, UsageRecord.estimated_cost_usd))
        ).where(UsageRecord.created_at >= month_start())
    )
    return float(value or 0.0)


def text_is_edited(page: Page) -> bool:
    """True when the stored processed text differs from the AI/original output."""
    if not page.processed_text:
        return False
    return page.manually_edited or text_hash(page.processed_text) != page.processed_hash


def selected_course(session: Session) -> Course | None:
    return session.scalar(select(Course).where(Course.is_selected.is_(True)))
