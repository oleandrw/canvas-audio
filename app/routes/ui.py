"""HTML views and the small HTMX fragments they poll.

The templates reuse the same presenter functions as the JSON API, so the two can
never disagree about what a page's status is.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, SECRET_KEYS, get_settings, get_store, mask
from app.database import get_session
from app.models.core import (
    Course,
    GenerationJob,
    JobItem,
    JobStatus,
    Module,
    Page,
    PageAudioStatus,
)
from app.pricing import load_pricing
from app.routes.settings import SECRET_LABELS, build_status
from app.schemas.costs import EstimateRequest
from app.security import SESSION_AUTH_KEY, is_authenticated, verify_password
from app.services import soundcloud_publish, soundcloud_service, update_service
from app.services.app_settings import load_preferences
from app.services.audio_service import format_bytes, format_duration
from app.services.cost_service import build_estimate
from app.services.hashing import audio_is_current, page_audio_status, status_label
from app.services.presenters import (
    item_status_label,
    job_out,
    month_start,
    page_detail,
    provider_label,
    selected_course,
    spent_this_month,
    usage_summary,
)
from app.services.sync_service import course_structure

router = APIRouter(tags=["ui"], include_in_schema=False)

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

NAV_ITEMS = [
    {"key": "dashboard", "href": "/", "emoji": "📊", "label": "Oversikt"},
    {"key": "canvas", "href": "/canvas", "emoji": "🎓", "label": "Canvas"},
    {"key": "generation", "href": "/generering", "emoji": "🎧", "label": "Generering"},
    {
        "key": "canvas_player",
        "href": "/publiser-canvas",
        "emoji": "📤",
        "label": "Publiser i Canvas",
    },
    {"key": "soundcloud", "href": "/soundcloud", "emoji": "☁️", "label": "SoundCloud"},
    {"key": "api", "href": "/api-nokler", "emoji": "🔑", "label": "API"},
    {"key": "settings", "href": "/innstillinger", "emoji": "⚙️", "label": "Innstillinger"},
    {"key": "update", "href": "/oppdatering", "emoji": "⬆️", "label": "Oppdatering"},
]

STATUS_BADGE_CLASS = {
    PageAudioStatus.UP_TO_DATE: "badge-ok",
    PageAudioStatus.NOT_GENERATED: "badge-muted",
    PageAudioStatus.CANVAS_CHANGED: "badge-warn",
    PageAudioStatus.TEXT_CHANGED: "badge-warn",
    PageAudioStatus.FAILED: "badge-error",
}

JOB_BADGE_CLASS = {
    JobStatus.QUEUED: "badge-brand",
    JobStatus.RUNNING: "badge-brand",
    JobStatus.COMPLETED: "badge-ok",
    JobStatus.COMPLETED_WITH_ERRORS: "badge-warn",
    JobStatus.FAILED: "badge-error",
    JobStatus.CANCELLED: "badge-muted",
}

ITEM_BADGE_CLASS = {
    "pending": "badge-muted",
    "cleaning": "badge-info",
    "ai_processing": "badge-info",
    "ready_for_tts": "badge-info",
    "generating_audio": "badge-brand",
    "done": "badge-ok",
    "skipped": "badge-muted",
    "failed": "badge-error",
}


def nb(value: Any) -> str:
    """Norwegian thousands separator: 286450 → '286 450'."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _status_badge(status: str) -> str:
    return STATUS_BADGE_CLASS.get(status, "badge-muted")


def _status_icon(status: str) -> str:
    icon, _label, _kind = status_label(status)
    return icon


def _ai_models_json() -> str:
    """Model choices for the front-end selects, with prices from pricing.toml."""
    pricing = load_pricing()
    payload: dict[str, list[dict[str, str]]] = {"none": []}
    for provider, models in (
        ("openai", pricing.openai_models),
        ("anthropic", pricing.anthropic_models),
    ):
        payload[provider] = [
            {
                "id": price.model_id,
                "label": price.label,
                "price": f"${price.input_per_mtok:g}/${price.output_per_mtok:g} per Mtok",
            }
            for price in models.values()
        ]
    return json.dumps(payload)


def render(request: Request, template: str, active: str, context: dict[str, Any]) -> HTMLResponse:
    settings = get_settings()
    base = {
        "request": request,
        "nav_items": NAV_ITEMS,
        "active_page": active,
        "show_nav": True,
        "auth_enabled": settings.auth_enabled,
        "nb": nb,
        "status_badge": _status_badge,
        "status_icon": _status_icon,
        "job_badge": lambda s: JOB_BADGE_CLASS.get(s, "badge-muted"),
        "item_badge": lambda s: ITEM_BADGE_CLASS.get(s, "badge-muted"),
        "provider_label": provider_label,
        "item_status_label": item_status_label,
        "ai_models_json": _ai_models_json(),
        "restart_pending": update_service.restart_pending(),
    }
    base.update(context)
    return templates.TemplateResponse(request, template, base)


def guard(request: Request) -> RedirectResponse | None:
    """UI pages redirect to the login form instead of returning a bare 401."""
    if is_authenticated(request):
        return None
    return RedirectResponse("/logg-inn", status_code=303)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


@router.get("/logg-inn", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "show_nav": False, "error": ""}
    )


@router.post("/logg-inn")
def login(request: Request, password: str = Form(default="")) -> Any:
    if not verify_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "show_nav": False, "error": "Feil passord."},
            status_code=401,
        )
    request.session[SESSION_AUTH_KEY] = True
    return RedirectResponse("/", status_code=303)


@router.post("/logg-ut")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/logg-inn", status_code=303)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


def _course_stats(session: Session, course: Course) -> dict[str, Any]:
    pages = list(
        session.execute(select(Page).where(Page.course_id == course.id, Page.archived.is_(False)))
        .scalars()
        .all()
    )
    statuses = [page_audio_status(p) for p in pages]
    generated = sum(1 for p in pages if p.audio is not None)
    total_seconds = sum((p.audio.duration_seconds if p.audio else 0.0) for p in pages)
    total_bytes = sum((p.audio.size_bytes if p.audio else 0) for p in pages)
    not_generated = statuses.count(PageAudioStatus.NOT_GENERATED)
    changed = statuses.count(PageAudioStatus.CANVAS_CHANGED) + statuses.count(
        PageAudioStatus.TEXT_CHANGED
    )
    module_count = int(
        session.scalar(select(func.count(Module.id)).where(Module.course_id == course.id)) or 0
    )
    return {
        "modules": module_count,
        "pages": len(pages),
        "generated": generated,
        "not_generated": not_generated,
        "changed": changed,
        "remaining": not_generated + changed,
        "total_duration_label": format_duration(total_seconds),
        "total_size_label": format_bytes(total_bytes),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect

    settings = get_settings()
    course = selected_course(session)
    stats = _course_stats(session, course) if course else {}

    active = session.scalar(
        select(GenerationJob)
        .where(GenerationJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        .order_by(GenerationJob.id.desc())
    ) or session.scalar(select(GenerationJob).order_by(GenerationJob.id.desc()))

    jobs = (
        session.execute(select(GenerationJob).order_by(GenerationJob.id.desc()).limit(8))
        .scalars()
        .all()
    )

    failed_pages = []
    if course:
        failed_pages = list(
            session.execute(
                select(Page)
                .where(
                    Page.course_id == course.id,
                    Page.archived.is_(False),
                    Page.last_error != "",
                )
                .limit(10)
            )
            .scalars()
            .all()
        )

    spent = spent_this_month(session)
    prefs = load_preferences(session)
    budget = prefs.monthly_budget_usd

    missing = []
    if not settings.canvas_url or not settings.canvas_token:
        missing.append("Canvas")
    if not settings.elevenlabs_api_key:
        missing.append("ElevenLabs")

    from app.schemas.costs import UsageResponse

    usage = UsageResponse(
        this_month=usage_summary(session, scope="month", since=month_start()),
        all_time=usage_summary(session, scope="all"),
        budget_usd=budget,
        budget_used_percent=int(min(spent / budget * 100, 999)) if budget > 0 else 0,
    )

    return render(
        request,
        "dashboard.html",
        "dashboard",
        {
            "course": course,
            "stats": stats,
            "job": job_out(active, include_items=False) if active else None,
            "jobs": [job_out(j, include_items=False) for j in jobs],
            "failed_pages": failed_pages,
            "usage": usage,
            "soundcloud": _soundcloud_summary(session),
            "credentials_ok": not missing,
            "missing_summary": ("Mangler " + " og ".join(missing) + "-oppsett." if missing else ""),
        },
    )


def _soundcloud_summary(session: Session) -> dict[str, Any]:
    from app.models.core import SoundCloudPlaylist, SoundCloudTrack

    settings = get_settings()
    configured = bool(settings.soundcloud_client_id and settings.soundcloud_client_secret)
    connected = soundcloud_service.is_connected(session)
    detail = ""
    if not configured:
        detail = "Valgfri integrasjon som ikke er konfigurert."
    elif not connected:
        detail = "Konfigurert, men ikke tilkoblet."
    return {
        "configured": configured,
        "connected": connected,
        "account_name": soundcloud_service.stored_account_name(session),
        "track_count": int(session.scalar(select(func.count(SoundCloudTrack.id))) or 0),
        "playlist_count": int(session.scalar(select(func.count(SoundCloudPlaylist.id))) or 0),
        "detail": detail,
    }


@router.get("/canvas", response_class=HTMLResponse)
def canvas_view(request: Request, session: Session = Depends(get_session)) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect

    settings = get_settings()
    course = selected_course(session)
    structure = course_structure(session, course.id) if course else {"groups": [], "archived": []}

    groups = []
    for group in structure.get("groups", []):
        summaries = []
        ready = 0
        for page in group["pages"]:
            status = page_audio_status(page)
            if status == PageAudioStatus.UP_TO_DATE:
                ready += 1
            _icon, label, _kind = status_label(status)
            summaries.append(
                {
                    "id": page.id,
                    "title": page.title,
                    "characters": page.character_count,
                    "words": page.word_count,
                    "status": status,
                    "status_label": label,
                }
            )
        groups.append({"module": group["module"], "pages": summaries, "ready": ready})

    return render(
        request,
        "canvas.html",
        "canvas",
        {
            "course": course,
            "groups": groups,
            "archived": structure.get("archived", []),
            "stats": _course_stats(session, course) if course else {},
            "prefs": load_preferences(session),
            "canvas_configured": bool(settings.canvas_url and settings.canvas_token),
        },
    )


@router.get("/side/{page_id}", response_class=HTMLResponse)
def page_view(request: Request, page_id: int, session: Session = Depends(get_session)) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect

    page = session.get(Page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Fant ikke siden.")

    detail = page_detail(page)
    return render(
        request,
        "page_detail.html",
        "canvas",
        {
            "page": detail,
            "prefs": load_preferences(session),
            "size_label": format_bytes(detail.audio.size_bytes) if detail.audio else "",
        },
    )


def _generation_rows(session: Session, job: GenerationJob | None) -> list[dict[str, Any]]:
    item_by_page: dict[int, JobItem] = {}
    if job is not None:
        item_by_page = {item.page_id: item for item in job.items}

    if job is not None:
        pages = [item.page for item in job.items if item.page is not None]
    else:
        course = selected_course(session)
        if course is None:
            return []
        pages = list(
            session.execute(
                select(Page)
                .where(Page.course_id == course.id, Page.archived.is_(False))
                .order_by(Page.module_id, Page.position, Page.title)
            )
            .scalars()
            .all()
        )

    rows: list[dict[str, Any]] = []
    for page in pages:
        status = page_audio_status(page)
        _icon, label, _kind = status_label(status)
        item = item_by_page.get(page.id)
        rows.append(
            {
                "page": page,
                "module_name": page.module.name if page.module else "Uten modul",
                "status": status,
                "status_label": label,
                "item_status": item.status if item else "",
                "item_status_label": item_status_label(item.status) if item else "",
                "message": item.message if item else page.last_error,
            }
        )
    return rows


@router.get("/generering", response_class=HTMLResponse)
def generation_view(
    request: Request,
    jobb: int | None = Query(default=None),
    session: Session = Depends(get_session),
) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect

    job = session.get(GenerationJob, jobb) if jobb else None
    if job is None:
        job = session.scalar(
            select(GenerationJob)
            .where(GenerationJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .order_by(GenerationJob.id.desc())
        ) or session.scalar(select(GenerationJob).order_by(GenerationJob.id.desc()))

    jobs = (
        session.execute(select(GenerationJob).order_by(GenerationJob.id.desc()).limit(8))
        .scalars()
        .all()
    )

    return render(
        request,
        "generation.html",
        "generation",
        {
            "job": job_out(job, include_items=False) if job else None,
            "jobs": [job_out(j, include_items=False) for j in jobs],
            "rows": _generation_rows(session, job),
        },
    )


@router.get("/soundcloud", response_class=HTMLResponse)
def soundcloud_view(request: Request, session: Session = Depends(get_session)) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect

    summary = _soundcloud_summary(session)
    prefs = load_preferences(session)
    course = selected_course(session)

    modules: list[dict[str, Any]] = []
    if course is not None:
        for module in (
            session.execute(
                select(Module).where(Module.course_id == course.id).order_by(Module.position)
            )
            .scalars()
            .all()
        ):
            pages = [p for p in module.pages if not p.archived]
            spoken = [p for p in pages if p.tts_text.strip()]
            current = sum(1 for p in spoken if audio_is_current(p))
            playlist = module.playlist
            modules.append(
                {
                    "id": module.id,
                    "name": module.name,
                    "pages": len(spoken),
                    "with_audio": current,
                    "complete": bool(spoken) and current == len(spoken),
                    "uploaded": sum(1 for p in pages if p.soundcloud_track is not None),
                    "playlist_id": playlist.playlist_id if playlist else "",
                    "playlist_title": playlist.title if playlist else "",
                    "playlist_url": playlist.permalink_url if playlist else "",
                }
            )

    return render(
        request,
        "soundcloud.html",
        "soundcloud",
        {
            "sc": {**summary, "sharing": prefs.soundcloud_sharing},
            "modules": modules,
            "auto_publish": prefs.soundcloud_auto_publish,
            "last_publish": soundcloud_publish.last_publish(session),
        },
    )


def _credential_context() -> dict[str, Any]:
    """What the field macros need to render a credential: mask, source and lock."""
    store = get_store()

    def secret_info(key: str) -> dict[str, Any]:
        value = store.get(key)
        return {
            "key": key,
            "label": SECRET_LABELS.get(key, key),
            "configured": bool(value),
            "masked": mask(value),
            "locked": store.is_locked(key),
        }

    locked_keys = [SECRET_LABELS.get(key, key) for key in SECRET_KEYS if store.is_locked(key)] + [
        key
        for key in ("CANVAS_URL", "SOUNDCLOUD_CLIENT_ID", "SOUNDCLOUD_REDIRECT_URI")
        if store.is_locked(key)
    ]
    return {"secret_info": secret_info, "locked": store.is_locked, "env_locked_keys": locked_keys}


@router.get("/api-nokler", response_class=HTMLResponse)
def api_keys_view(request: Request, session: Session = Depends(get_session)) -> Any:
    """Keys and addresses — everything that is written to `.env`."""
    if (redirect := guard(request)) is not None:
        return redirect

    return render(
        request,
        "api_keys.html",
        "api",
        {"status": build_status(session), **_credential_context()},
    )


@router.get("/innstillinger", response_class=HTMLResponse)
def settings_view(request: Request, session: Session = Depends(get_session)) -> Any:
    """Choices — everything that is stored in the database."""
    if (redirect := guard(request)) is not None:
        return redirect

    return render(
        request,
        "settings.html",
        "settings",
        {
            "prefs": load_preferences(session),
            "elevenlabs_ready": bool(get_settings().elevenlabs_api_key),
        },
    )


# --------------------------------------------------------------------------- #
# HTMX / fetch fragments
# --------------------------------------------------------------------------- #


@router.get("/fragments/job-progress", response_class=HTMLResponse)
def fragment_job_progress(request: Request, session: Session = Depends(get_session)) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect
    job = session.scalar(
        select(GenerationJob)
        .where(GenerationJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        .order_by(GenerationJob.id.desc())
    ) or session.scalar(select(GenerationJob).order_by(GenerationJob.id.desc()))
    return render(
        request,
        "partials/job_progress.html",
        "dashboard",
        {"job": job_out(job, include_items=False) if job else None},
    )


@router.get("/fragments/generation-rows", response_class=HTMLResponse)
def fragment_generation_rows(
    request: Request,
    jobb: int | None = Query(default=None),
    session: Session = Depends(get_session),
) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect
    job = session.get(GenerationJob, jobb) if jobb else None
    return render(
        request,
        "partials/generation_table.html",
        "generation",
        {
            "job": job_out(job, include_items=False) if job else None,
            "rows": _generation_rows(session, job),
        },
    )


@router.post("/fragments/estimate")
def fragment_estimate(
    request: Request, payload: EstimateRequest, session: Session = Depends(get_session)
) -> Any:
    """Returns rendered HTML plus the flags the Canvas page needs to enable buttons."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Ikke innlogget")

    estimate = build_estimate(session, payload)
    prefs = load_preferences(session)
    tts_ready = bool((payload.tts_model or prefs.tts_model_id) and prefs.voice_id)

    html = render(
        request,
        "partials/estimate.html",
        "canvas",
        {"estimate": estimate, "tts_ready": tts_ready},
    ).body.decode("utf-8")

    return {
        "html": html,
        "tts_ready": tts_ready,
        "total_usd": estimate.total_usd,
        "requires_confirmation": estimate.requires_confirmation,
    }
