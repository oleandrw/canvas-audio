"""Canvas player: choose a design, then push audio into the course's pages.

Publishing changes the Canvas course, so it only ever starts from an explicit
request, runs in the background, and reports per page. The page view lives here
too, next to the API it drives.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session, run_db
from app.models.core import Page
from app.routes.ui import guard, render
from app.schemas.canvas_player import (
    PageOutcomeOut,
    PlayerDesignIn,
    PlayerPreview,
    PlayerPreviewRequest,
    PublishRunOut,
    PublishRunRequest,
    PublishStatus,
)
from app.schemas.common import OkResponse
from app.security import require_auth
from app.services import canvas_publish
from app.services.canvas_player import COLORS, ICONS, STYLES, PlayerDesign, player_html
from app.services.hashing import audio_is_current
from app.services.presenters import selected_course

router = APIRouter(
    prefix="/api/canvas-player", tags=["canvas-player"], dependencies=[Depends(require_auth)]
)
page_router = APIRouter(tags=["ui"], include_in_schema=False)

PREVIEW_EXCERPT = 420


def _design(payload: PlayerDesignIn) -> PlayerDesign:
    return PlayerDesign(
        style=payload.style, color=payload.color, label=payload.label, icon=payload.icon
    )


def run_out(run: canvas_publish.PublishRun) -> PublishRunOut:
    return PublishRunOut(
        id=run.id,
        action=run.action,
        course_name=run.course_name,
        total=run.total,
        done=run.done,
        current=run.current,
        running=run.running,
        error=run.error,
        summary=run.summary(),
        counts=run.counts(),
        outcomes=[PageOutcomeOut(**asdict(outcome)) for outcome in run.outcomes],
    )


@router.put("/design", response_model=OkResponse)
def save_design(payload: PlayerDesignIn, session: Session = Depends(get_session)) -> OkResponse:
    canvas_publish.save_design(session, _design(payload), payload.folder)
    session.commit()
    return OkResponse(
        message="Utseendet er lagret. Sider som allerede har avspiller, oppdateres neste gang "
        "du publiserer."
    )


@router.post("/preview", response_model=PlayerPreview)
def preview(payload: PlayerPreviewRequest) -> PlayerPreview:
    src = f"/api/audio/{payload.page_id}" if payload.page_id else ""
    return PlayerPreview(html=player_html(_design(payload), src))


@router.post("/runs", response_model=PublishRunOut)
async def start_run(payload: PublishRunRequest) -> PublishRunOut:
    settings = get_settings()
    if not (settings.canvas_url and settings.canvas_token):
        raise HTTPException(
            status_code=400, detail="Canvas-URL og token må settes i Innstillinger."
        )
    current = canvas_publish.current_run()
    if current is not None and current.running:
        raise HTTPException(status_code=409, detail="En publisering til Canvas pågår allerede.")

    plan = await run_db(
        lambda session: canvas_publish.prepare(session, payload.action, payload.module_ids)
    )
    if not plan.tasks:
        detail = (
            "Ingen av sidene har avspiller i Canvas."
            if payload.action == "remove"
            else "Ingen sider har oppdatert lyd å publisere."
        )
        raise HTTPException(status_code=400, detail=detail)
    try:
        run = canvas_publish.new_run(payload.action, plan)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    canvas_publish.launch(run, plan)
    return run_out(run)


@router.get("/runs/current", response_model=PublishStatus)
def current_run() -> PublishStatus:
    run = canvas_publish.current_run()
    return PublishStatus(run=run_out(run) if run else None)


@page_router.get("/publiser-canvas", response_class=HTMLResponse)
def canvas_player_view(request: Request, session: Session = Depends(get_session)) -> Any:
    if (redirect := guard(request)) is not None:
        return redirect

    settings = get_settings()
    course = selected_course(session)
    design = canvas_publish.load_design(session)
    modules = canvas_publish.module_rows(session, course) if course else []

    sample: dict[str, Any] | None = None
    if course is not None:
        pages = session.execute(
            select(Page)
            .where(Page.course_id == course.id, Page.archived.is_(False))
            .order_by(Page.module_id, Page.position)
        ).scalars()
        page = next((p for p in pages if p.audio is not None and audio_is_current(p)), None)
        if page is not None:
            excerpt = page.original_text[:PREVIEW_EXCERPT].rsplit(" ", 1)[0]
            sample = {"id": page.id, "title": page.title, "excerpt": excerpt}

    run = canvas_publish.current_run()
    totals = {
        key: sum(row[key] for row in modules)
        for key in ("pages", "ready", "published", "outdated", "failed", "on_canvas")
    }
    return render(
        request,
        "canvas_player.html",
        "canvas_player",
        {
            "course": course,
            "modules": modules,
            "totals": totals,
            "design": design,
            "folder": canvas_publish.load_folder(session),
            "styles": STYLES,
            "colors": COLORS,
            "icons": ICONS,
            "sample": sample,
            "preview_html": player_html(design, f"/api/audio/{sample['id']}" if sample else ""),
            "canvas_configured": bool(settings.canvas_url and settings.canvas_token),
            "run": run_out(run).model_dump(mode="json") if run else None,
        },
    )
