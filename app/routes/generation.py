"""Creating and following generation jobs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_session
from app.jobs.queue import get_queue
from app.logging_config import get_logger
from app.models.core import GenerationJob, ItemStatus, JobItem, JobStatus
from app.schemas.common import OkResponse
from app.schemas.costs import EstimateRequest
from app.schemas.generation import (
    ConfirmationRequired,
    GenerationAccepted,
    GenerationRequest,
    JobOut,
)
from app.security import require_auth
from app.services.app_settings import load_preferences
from app.services.cost_service import build_estimate
from app.services.errors import ConfigurationError
from app.services.presenters import job_out, selected_course
from app.services.selection import resolve_pages

logger = get_logger("app.routes.generation")

router = APIRouter(
    prefix="/api/generation", tags=["generation"], dependencies=[Depends(require_auth)]
)


@router.post(
    "", response_model=GenerationAccepted, responses={409: {"model": ConfirmationRequired}}
)
async def create_job(
    payload: GenerationRequest, session: Session = Depends(get_session)
) -> GenerationAccepted | JSONResponse:
    prefs = load_preferences(session)

    voice_id = payload.voice_id or prefs.voice_id
    voice_name = payload.voice_name or prefs.voice_name
    tts_model = payload.tts_model or prefs.tts_model_id
    ai_provider = payload.ai_provider or prefs.ai_provider
    ai_model = payload.ai_model or prefs.ai_model

    if not voice_id or not tts_model:
        raise ConfigurationError(
            "Velg både ElevenLabs-stemme og -modell før du genererer lyd.", provider="elevenlabs"
        )

    pages = resolve_pages(session, mode=payload.mode, page_ids=payload.page_ids)
    if not pages:
        raise HTTPException(status_code=400, detail="Ingen sider er valgt.")

    estimate = build_estimate(
        session,
        EstimateRequest(
            page_ids=[p.id for p in pages],
            mode="selected",
            ai_provider=ai_provider,
            ai_model=ai_model,
            tts_model=tts_model,
            force_regenerate=payload.force_regenerate,
        ),
    )

    # Guard rails: an unexpectedly large bill must be acknowledged first.
    if not payload.confirmed and (estimate.requires_confirmation or estimate.exceeds_budget):
        reason = "budget" if estimate.exceeds_budget else "threshold"
        message = (
            f"Dette vil anslagsvis koste ${estimate.total_usd:.2f}. Vil du fortsette?"
            if reason == "threshold"
            else (
                f"Dette vil anslagsvis koste ${estimate.total_usd:.2f} og bringe forbruket "
                f"denne måneden til ${estimate.spent_this_month_usd + estimate.total_usd:.2f} "
                f"av budsjettet på ${estimate.budget_usd:.2f}. Vil du fortsette?"
            )
        )
        body = ConfirmationRequired(
            reason=reason,
            estimated_cost_usd=estimate.total_usd,
            threshold_usd=estimate.threshold_usd,
            budget_usd=estimate.budget_usd,
            spent_this_month_usd=estimate.spent_this_month_usd,
            message=message,
        )
        return JSONResponse(status_code=409, content=body.model_dump())

    course = selected_course(session)
    voice_settings = (
        payload.voice_settings.model_dump() if payload.voice_settings else prefs.voice_settings
    )

    job = GenerationJob(
        course_id=course.id if course else None,
        status=JobStatus.QUEUED,
        ai_provider=ai_provider,
        ai_model=ai_model,
        voice_id=voice_id,
        voice_name=voice_name,
        tts_model=tts_model,
        voice_settings=voice_settings,
        force_regenerate=payload.force_regenerate,
        total_items=len(pages),
        estimated_cost_usd=estimate.total_usd,
    )
    session.add(job)
    session.flush()
    for page in pages:
        session.add(
            JobItem(
                job_id=job.id,
                page_id=page.id,
                status=ItemStatus.PENDING,
                characters=page.character_count,
            )
        )
    session.commit()

    await get_queue().enqueue(job.id)
    logger.info(
        "Generation job created id=%s pages=%d estimate=$%.2f",
        job.id,
        len(pages),
        estimate.total_usd,
    )
    return GenerationAccepted(
        job_id=job.id,
        total_items=len(pages),
        estimated_cost_usd=estimate.total_usd,
        message=f"Jobb startet for {len(pages)} sider.",
    )


@router.get("", response_model=list[JobOut])
def list_jobs(
    limit: int = Query(default=10, ge=1, le=100), session: Session = Depends(get_session)
) -> list[JobOut]:
    jobs = (
        session.execute(select(GenerationJob).order_by(GenerationJob.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [job_out(job, include_items=False) for job in jobs]


@router.get("/active", response_model=JobOut | None)
def active_job(session: Session = Depends(get_session)) -> JobOut | None:
    job = session.scalar(
        select(GenerationJob)
        .where(GenerationJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        .order_by(GenerationJob.id.desc())
    )
    return job_out(job) if job else None


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(get_session)) -> JobOut:
    job = session.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Fant ikke jobben.")
    return job_out(job)


@router.post("/{job_id}/cancel", response_model=OkResponse)
def cancel_job(job_id: int, session: Session = Depends(get_session)) -> OkResponse:
    job = session.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Fant ikke jobben.")
    if job.status in JobStatus.TERMINAL:
        return OkResponse(ok=False, message="Jobben er allerede avsluttet.")
    get_queue().cancel(job_id)
    return OkResponse(message="Avbryter jobben etter gjeldende side.")


@router.post("/{job_id}/retry-failed", response_model=GenerationAccepted)
async def retry_failed(
    job_id: int, session: Session = Depends(get_session)
) -> GenerationAccepted | JSONResponse:
    """Re-queue only the pages that failed in an earlier job."""
    job = session.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Fant ikke jobben.")
    failed_page_ids = [item.page_id for item in job.items if item.status == ItemStatus.FAILED]
    if not failed_page_ids:
        raise HTTPException(status_code=400, detail="Ingen feilede sider i denne jobben.")

    return await create_job(
        GenerationRequest(
            page_ids=failed_page_ids,
            mode="selected",
            ai_provider=job.ai_provider,
            ai_model=job.ai_model,
            voice_id=job.voice_id,
            voice_name=job.voice_name,
            tts_model=job.tts_model,
            force_regenerate=True,
            confirmed=True,
        ),
        session,
    )
