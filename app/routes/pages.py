"""Pages: list, inspect, edit the narration text, process or (re)generate one page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session
from app.jobs.queue import get_queue
from app.logging_config import get_logger
from app.models.core import AIProvider, GenerationJob, ItemStatus, JobItem, JobStatus, Page
from app.schemas.canvas import PageSummaryOut
from app.schemas.common import OkResponse
from app.schemas.generation import GenerationAccepted
from app.schemas.pages import PageDetail, PageProcessRequest, PageTextUpdate
from app.security import require_auth
from app.services import anthropic_service, openai_service
from app.services.app_settings import load_preferences
from app.services.errors import ConfigurationError, ServiceError
from app.services.hashing import text_hash
from app.services.presenters import page_detail, page_summary, selected_course
from app.services.text_service import html_to_text

logger = get_logger("app.routes.pages")

router = APIRouter(prefix="/api/pages", tags=["pages"], dependencies=[Depends(require_auth)])


def _get_page(session: Session, page_id: int) -> Page:
    page = session.get(Page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Fant ikke siden.")
    return page


@router.get("", response_model=list[PageSummaryOut])
def list_pages(
    course_id: int | None = Query(default=None),
    include_archived: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> list[PageSummaryOut]:
    if course_id is None:
        course = selected_course(session)
        if course is None:
            return []
        course_id = course.id

    query = select(Page).where(Page.course_id == course_id)
    if not include_archived:
        query = query.where(Page.archived.is_(False))
    pages = (
        session.execute(query.order_by(Page.module_id, Page.position, Page.title)).scalars().all()
    )
    return [page_summary(p) for p in pages]


@router.get("/{page_id}", response_model=PageDetail)
def get_page(page_id: int, session: Session = Depends(get_session)) -> PageDetail:
    return page_detail(_get_page(session, page_id))


@router.put("/{page_id}/text", response_model=PageDetail)
def update_page_text(
    page_id: int, payload: PageTextUpdate, session: Session = Depends(get_session)
) -> PageDetail:
    """Save a manual edit. This text is what text-to-speech will receive."""
    page = _get_page(session, page_id)
    text = payload.processed_text.strip()

    page.processed_text = text
    page.processed_hash = text_hash(text)
    page.manually_edited = bool(text) and text != page.original_text
    session.commit()
    session.refresh(page)
    logger.info("Page text edited page_id=%s characters=%d", page_id, len(text))
    return page_detail(page)


@router.post("/{page_id}/reset-text", response_model=PageDetail)
def reset_page_text(page_id: int, session: Session = Depends(get_session)) -> PageDetail:
    """Discard the processed/edited text and fall back to the cleaned original."""
    page = _get_page(session, page_id)
    page.processed_text = ""
    page.processed_hash = ""
    page.processed_by = AIProvider.NONE
    page.processed_model = ""
    page.processed_source_hash = ""
    page.prompt_version = ""
    page.manually_edited = False
    session.commit()
    session.refresh(page)
    return page_detail(page)


@router.post("/{page_id}/clean", response_model=PageDetail)
def reclean_page(page_id: int, session: Session = Depends(get_session)) -> PageDetail:
    """Re-run the HTML → text cleaner on the stored Canvas HTML. Costs nothing."""
    page = _get_page(session, page_id)
    page.original_text = html_to_text(page.html)
    page.original_hash = text_hash(page.original_text)
    session.commit()
    session.refresh(page)
    return page_detail(page)


@router.post("/{page_id}/process", response_model=PageDetail)
async def process_page(page_id: int, payload: PageProcessRequest) -> PageDetail:
    """Run AI text processing for a single page (a paid call)."""
    from app.database import run_db

    settings = get_settings()

    def _load(session: Session) -> tuple[str, str, str, bool]:
        page = _get_page(session, page_id)
        return page.title, page.original_text, page.original_hash, page.manually_edited

    title, original_text, original_hash, manually_edited = await run_db(_load)

    if manually_edited and not payload.force:
        raise HTTPException(
            status_code=409,
            detail="Teksten er redigert manuelt. Bruk «Overskriv» hvis du vil kjøre AI likevel.",
        )
    if not original_text.strip():
        raise HTTPException(status_code=400, detail="Siden har ingen tekst å bearbeide.")

    provider = payload.ai_provider
    model = payload.ai_model
    try:
        if provider == AIProvider.OPENAI:
            result = await openai_service.process_text(
                original_text, title=title, model=model, api_key=settings.openai_api_key
            )
        elif provider == AIProvider.ANTHROPIC:
            result = await anthropic_service.process_text(
                original_text, title=title, model=model, api_key=settings.anthropic_api_key
            )
        else:
            raise HTTPException(status_code=400, detail="Velg OpenAI eller Anthropic først.")
    except ServiceError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

    from app.jobs.pipeline import store_ai_result

    def _store(session: Session) -> PageDetail:
        store_ai_result(
            session,
            page_id,
            text=result.text,
            provider=result.provider,
            model=result.model,
            source_hash=original_hash,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            job_id=0,
        )
        return page_detail(_get_page(session, page_id))

    return await run_db(_store)


@router.post("/{page_id}/generate", response_model=GenerationAccepted)
async def generate_page(
    page_id: int, session: Session = Depends(get_session)
) -> GenerationAccepted:
    """Queue a one-page job using the saved preferences."""
    page = _get_page(session, page_id)
    prefs = load_preferences(session)
    if not prefs.tts_ready:
        raise ConfigurationError(
            "Velg ElevenLabs-stemme og -modell i Innstillinger først.", provider="elevenlabs"
        )

    job = GenerationJob(
        course_id=page.course_id,
        status=JobStatus.QUEUED,
        ai_provider=prefs.ai_provider,
        ai_model=prefs.ai_model,
        voice_id=prefs.voice_id,
        voice_name=prefs.voice_name,
        tts_model=prefs.tts_model_id,
        voice_settings=prefs.voice_settings,
        force_regenerate=True,
        total_items=1,
    )
    session.add(job)
    session.flush()
    session.add(JobItem(job_id=job.id, page_id=page.id, status=ItemStatus.PENDING))
    session.commit()

    await get_queue().enqueue(job.id)
    return GenerationAccepted(
        job_id=job.id, total_items=1, estimated_cost_usd=0.0, message="Lydgenerering startet."
    )


@router.post("/{page_id}/retry", response_model=GenerationAccepted)
async def retry_page(page_id: int, session: Session = Depends(get_session)) -> GenerationAccepted:
    """Retry a page that failed. Identical to a forced single-page generation."""
    page = _get_page(session, page_id)
    page.last_error = ""
    session.commit()
    return await generate_page(page_id, session)


@router.delete("/{page_id}/audio", response_model=OkResponse)
def delete_page_audio(page_id: int, session: Session = Depends(get_session)) -> OkResponse:
    from app.services.audio_service import delete_audio

    page = _get_page(session, page_id)
    if page.audio is None:
        return OkResponse(ok=False, message="Siden har ingen lydfil.")
    delete_audio(page.audio.relative_path)
    session.delete(page.audio)
    session.commit()
    return OkResponse(message="Lydfilen er slettet.")
