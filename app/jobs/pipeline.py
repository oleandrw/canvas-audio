"""The per-page generation pipeline.

One page moves through: clean text → optional AI rewrite → text-to-speech →
file on disk. Every paid step is preceded by a check that the work is actually
needed, and a failure on one page never stops the rest of the queue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import run_db
from app.logging_config import get_logger
from app.models.core import (
    AIProvider,
    GeneratedAudio,
    GenerationJob,
    ItemStatus,
    JobItem,
    JobStatus,
    Page,
    UsageRecord,
)
from app.pricing import load_pricing
from app.services import anthropic_service, elevenlabs_service, openai_service
from app.services.app_settings import load_preferences
from app.services.audio_service import build_relative_path, write_audio
from app.services.errors import InvalidContentError, ServiceError
from app.services.hashing import ai_result_is_reusable, audio_is_current, text_hash
from app.services.prompts import PROMPT_VERSION
from app.services.text_service import (
    ensure_plain_text,
    ensure_sentence_endings,
    html_to_text,
)

logger = get_logger("app.jobs.pipeline")


@dataclass(slots=True)
class JobConfig:
    job_id: int
    ai_provider: str
    ai_model: str
    voice_id: str
    voice_name: str
    tts_model: str
    voice_settings: dict[str, Any]
    force_regenerate: bool
    filename_style: str
    item_ids: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Small synchronous database helpers (always called through run_db)
# --------------------------------------------------------------------------- #


def load_job_config(session: Session, job_id: int) -> JobConfig | None:
    job = session.get(GenerationJob, job_id)
    if job is None:
        return None
    prefs = load_preferences(session)
    item_ids = [
        row.id
        for row in session.execute(
            select(JobItem).where(JobItem.job_id == job_id).order_by(JobItem.id)
        ).scalars()
    ]
    return JobConfig(
        job_id=job.id,
        ai_provider=job.ai_provider,
        ai_model=job.ai_model,
        voice_id=job.voice_id,
        voice_name=job.voice_name,
        tts_model=job.tts_model,
        voice_settings=job.voice_settings or {},
        force_regenerate=job.force_regenerate,
        filename_style=prefs.filename_style,
        item_ids=item_ids,
    )


def mark_job_running(session: Session, job_id: int) -> None:
    job = session.get(GenerationJob, job_id)
    if job is None:
        return
    job.status = JobStatus.RUNNING
    job.started_at = job.started_at or datetime.now(UTC)


def finish_job(session: Session, job_id: int, *, cancelled: bool = False) -> None:
    job = session.get(GenerationJob, job_id)
    if job is None:
        return
    job.finished_at = datetime.now(UTC)
    if cancelled:
        job.status = JobStatus.CANCELLED
    elif job.failed_items:
        job.status = JobStatus.COMPLETED_WITH_ERRORS
    else:
        job.status = JobStatus.COMPLETED


def set_item_status(session: Session, item_id: int, status: str, message: str = "") -> None:
    item = session.get(JobItem, item_id)
    if item is None:
        return
    item.status = status
    if message:
        item.message = message[:1000]
    if status != ItemStatus.PENDING and item.started_at is None:
        item.started_at = datetime.now(UTC)
    if status in ItemStatus.TERMINAL:
        item.finished_at = datetime.now(UTC)


def _bump_counter(job: GenerationJob, status: str) -> None:
    if status == ItemStatus.DONE:
        job.completed_items += 1
    elif status == ItemStatus.FAILED:
        job.failed_items += 1
    elif status == ItemStatus.SKIPPED:
        job.skipped_items += 1


def complete_item(session: Session, item_id: int, status: str, message: str = "") -> None:
    item = session.get(JobItem, item_id)
    if item is None:
        return
    set_item_status(session, item_id, status, message)
    job = session.get(GenerationJob, item.job_id)
    if job is not None:
        _bump_counter(job, status)
    page = session.get(Page, item.page_id)
    if page is not None:
        page.last_error = message[:1000] if status == ItemStatus.FAILED else ""


@dataclass(slots=True)
class PageWork:
    """Everything the async pipeline needs about a page, detached from the session."""

    page_id: int
    title: str
    original_text: str
    processed_text: str
    original_hash: str
    manually_edited: bool
    needs_tts: bool
    needs_ai: bool
    relative_path: str


def prepare_page(session: Session, item_id: int, config: JobConfig) -> PageWork | None:
    """Refresh derived text, decide what work is needed, and reserve a file path."""
    item = session.get(JobItem, item_id)
    if item is None:
        return None
    page = session.get(Page, item.page_id)
    if page is None:
        return None

    # Text may be missing if the page was synced before the cleaner ran.
    if not page.original_text and page.html:
        page.original_text = html_to_text(page.html)
        page.original_hash = text_hash(page.original_text)
        session.flush()

    needs_tts = config.force_regenerate or not audio_is_current(page)
    needs_ai = (
        config.ai_provider != AIProvider.NONE
        and needs_tts
        and not ai_result_is_reusable(page, config.ai_provider, config.ai_model)
    )
    relative_path = build_relative_path(page, style=config.filename_style) if needs_tts else ""
    item.characters = page.character_count
    session.flush()

    return PageWork(
        page_id=page.id,
        title=page.title,
        original_text=page.original_text,
        processed_text=page.processed_text,
        original_hash=page.original_hash,
        manually_edited=page.manually_edited,
        needs_tts=needs_tts,
        needs_ai=needs_ai,
        relative_path=relative_path,
    )


def store_ai_result(
    session: Session,
    page_id: int,
    *,
    text: str,
    provider: str,
    model: str,
    source_hash: str,
    input_tokens: int,
    output_tokens: int,
    job_id: int,
) -> None:
    page = session.get(Page, page_id)
    if page is None:
        return
    text = ensure_sentence_endings(text)
    page.processed_text = text
    page.processed_hash = text_hash(text)
    page.processed_by = provider
    page.processed_model = model
    page.processed_source_hash = source_hash
    page.prompt_version = PROMPT_VERSION
    page.manually_edited = False

    pricing = load_pricing()
    session.add(
        UsageRecord(
            provider=provider,
            model=model,
            operation="text_processing",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=pricing.ai_cost(provider, model, input_tokens, output_tokens),
            actual_cost_usd=pricing.ai_cost(provider, model, input_tokens, output_tokens),
            page_id=page_id,
            job_id=job_id,
        )
    )
    session.flush()


def store_audio_result(
    session: Session,
    page_id: int,
    *,
    stored: Any,
    text: str,
    voice_id: str,
    voice_name: str,
    model_id: str,
    voice_settings: dict[str, Any],
    characters: int,
    job_id: int,
) -> None:
    page = session.get(Page, page_id)
    if page is None:
        return
    audio = page.audio or GeneratedAudio(page_id=page_id)
    audio.relative_path = stored.relative_path
    audio.original_title = page.title
    audio.size_bytes = stored.size_bytes
    audio.duration_seconds = stored.duration_seconds
    audio.file_hash = stored.file_hash
    audio.voice_id = voice_id
    audio.voice_name = voice_name
    audio.model_id = model_id
    audio.voice_settings = voice_settings
    audio.character_count = characters
    audio.source_text_hash = text_hash(text)
    audio.source_canvas_hash = page.original_hash
    audio.generated_at = datetime.now(UTC)
    if page.audio is None:
        session.add(audio)

    pricing = load_pricing()
    cost = pricing.tts_cost(model_id, characters)
    session.add(
        UsageRecord(
            provider="elevenlabs",
            model=model_id,
            operation="text_to_speech",
            characters=characters,
            estimated_cost_usd=cost,
            actual_cost_usd=cost,
            page_id=page_id,
            job_id=job_id,
        )
    )
    page.last_error = ""
    session.flush()


# --------------------------------------------------------------------------- #
# Async pipeline
# --------------------------------------------------------------------------- #


async def _run_ai(work: PageWork, config: JobConfig) -> Any:
    settings = get_settings()
    source_text = work.original_text
    if config.ai_provider == AIProvider.OPENAI:
        return await openai_service.process_text(
            source_text,
            title=work.title,
            model=config.ai_model,
            api_key=settings.openai_api_key,
        )
    if config.ai_provider == AIProvider.ANTHROPIC:
        return await anthropic_service.process_text(
            source_text,
            title=work.title,
            model=config.ai_model,
            api_key=settings.anthropic_api_key,
        )
    raise InvalidContentError(f"Ukjent AI-leverandør: {config.ai_provider}")


async def process_item(
    item_id: int,
    config: JobConfig,
    *,
    tts_client: Any,
    index: int,
    total: int,
) -> str:
    """Run one page end to end. Returns the terminal :class:`ItemStatus`."""
    work = await run_db(lambda session: prepare_page(session, item_id, config))
    if work is None:
        await run_db(
            lambda session: complete_item(
                session, item_id, ItemStatus.FAILED, "Fant ikke siden i databasen."
            )
        )
        return ItemStatus.FAILED

    logger.info("Processing page %d/%d title=%s", index, total, work.title)

    if not work.needs_tts:
        await run_db(
            lambda session: complete_item(
                session, item_id, ItemStatus.SKIPPED, "Lyden er allerede oppdatert."
            )
        )
        return ItemStatus.SKIPPED

    try:
        await run_db(lambda session: set_item_status(session, item_id, ItemStatus.CLEANING))
        if not work.original_text.strip():
            raise InvalidContentError(
                "Siden har ingen tekst. Synkroniser Canvas på nytt, eller hopp over siden."
            )

        text_for_tts = work.processed_text or work.original_text

        if work.needs_ai:
            await run_db(
                lambda session: set_item_status(session, item_id, ItemStatus.AI_PROCESSING)
            )
            result = await _run_ai(work, config)
            text_for_tts = ensure_sentence_endings(result.text)
            await run_db(
                lambda session: store_ai_result(
                    session,
                    work.page_id,
                    text=text_for_tts,
                    provider=result.provider,
                    model=result.model,
                    source_hash=work.original_hash,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    job_id=config.job_id,
                )
            )

        await run_db(lambda session: set_item_status(session, item_id, ItemStatus.READY_FOR_TTS))
        clean_text = ensure_plain_text(text_for_tts, context="teksten som skal leses opp")

        await run_db(lambda session: set_item_status(session, item_id, ItemStatus.GENERATING_AUDIO))
        synthesis = await tts_client.synthesize(
            clean_text,
            voice_id=config.voice_id,
            model_id=config.tts_model,
            voice_settings=config.voice_settings or None,
        )
        stored = write_audio(work.relative_path, synthesis.audio)

        await run_db(
            lambda session: store_audio_result(
                session,
                work.page_id,
                stored=stored,
                text=clean_text,
                voice_id=config.voice_id,
                voice_name=config.voice_name,
                model_id=config.tts_model,
                voice_settings=config.voice_settings,
                characters=synthesis.characters,
                job_id=config.job_id,
            )
        )
        await run_db(lambda session: complete_item(session, item_id, ItemStatus.DONE))
        return ItemStatus.DONE

    except ServiceError as exc:
        message = str(exc)
        logger.warning("Page failed title=%s error=%s", work.title, message)
        await run_db(lambda session: complete_item(session, item_id, ItemStatus.FAILED, message))
        return ItemStatus.FAILED
    except Exception as exc:  # a bug must not take the whole queue down
        message = f"Uventet feil: {exc}"
        logger.exception("Unexpected error while processing page %s", work.title)
        await run_db(lambda session: complete_item(session, item_id, ItemStatus.FAILED, message))
        return ItemStatus.FAILED


async def run_job(
    job_id: int,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    tts_client: Any | None = None,
) -> None:
    """Execute every item of a job in order."""
    config = await run_db(lambda session: load_job_config(session, job_id))
    if config is None:
        logger.warning("Job %s disappeared before it could run", job_id)
        return

    await run_db(lambda session: mark_job_running(session, job_id))
    logger.info(
        "Generation job started id=%s pages=%d ai=%s voice=%s model=%s",
        job_id,
        len(config.item_ids),
        config.ai_provider,
        config.voice_name or config.voice_id,
        config.tts_model,
    )

    settings = get_settings()
    owns_client = tts_client is None
    cancelled = False
    try:
        client = tts_client or elevenlabs_service.ElevenLabsClient(settings.elevenlabs_api_key)
    except ServiceError as exc:
        message = str(exc)
        await run_db(lambda session: _fail_job(session, job_id, message))
        return

    try:
        total = len(config.item_ids)
        for index, item_id in enumerate(config.item_ids, start=1):
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                await run_db(lambda session: _cancel_remaining(session, job_id))
                break
            await process_item(item_id, config, tts_client=client, index=index, total=total)
    finally:
        if owns_client:
            await client.aclose()
        await run_db(lambda session: finish_job(session, job_id, cancelled=cancelled))
        logger.info("Generation job finished id=%s cancelled=%s", job_id, cancelled)


def _fail_job(session: Session, job_id: int, message: str) -> None:
    job = session.get(GenerationJob, job_id)
    if job is None:
        return
    job.status = JobStatus.FAILED
    job.error = message[:1000]
    job.finished_at = datetime.now(UTC)
    for item in job.items:
        if item.status not in ItemStatus.TERMINAL:
            item.status = ItemStatus.FAILED
            item.message = message[:1000]
            job.failed_items += 1


def _cancel_remaining(session: Session, job_id: int) -> None:
    job = session.get(GenerationJob, job_id)
    if job is None:
        return
    for item in job.items:
        if item.status not in ItemStatus.TERMINAL:
            item.status = ItemStatus.SKIPPED
            item.message = "Avbrutt av bruker."
            job.skipped_items += 1
