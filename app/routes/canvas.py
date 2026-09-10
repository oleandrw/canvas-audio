"""Canvas: list courses, inspect one course, and synchronise it locally."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session
from app.logging_config import get_logger
from app.models.core import Course
from app.schemas.canvas import CourseOption, CourseOut, SyncRequest, SyncResponse
from app.security import require_auth
from app.services.canvas_service import CanvasClient
from app.services.presenters import course_out, selected_course
from app.services.sync_service import sync_course

logger = get_logger("app.routes.canvas")

router = APIRouter(prefix="/api/canvas", tags=["canvas"], dependencies=[Depends(require_auth)])


@router.get("/courses", response_model=list[CourseOption])
async def list_courses() -> list[CourseOption]:
    """List courses the token can see.

    Some Canvas tokens cannot list courses at all; the GUI then falls back to a
    manually entered course id, so a failure here is not fatal.
    """
    settings = get_settings()
    async with CanvasClient(settings.canvas_url, settings.canvas_token) as client:
        courses = await client.list_courses()
    return [CourseOption(id=c.id, name=c.name, course_code=c.course_code) for c in courses]


@router.get("/courses/{course_id}", response_model=CourseOut)
def get_local_course(course_id: int, session: Session = Depends(get_session)) -> CourseOut:
    """The locally stored structure for a synced course."""
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Emnet er ikke synkronisert ennå.")
    return course_out(session, course)


@router.get("/selected", response_model=CourseOut | None)
def get_selected_course(session: Session = Depends(get_session)) -> CourseOut | None:
    course = selected_course(session)
    return course_out(session, course) if course else None


@router.post("/sync", response_model=SyncResponse)
async def sync(payload: SyncRequest) -> SyncResponse:
    settings = get_settings()
    result = await sync_course(
        base_url=settings.canvas_url,
        token=settings.canvas_token,
        canvas_course_id=payload.course_id,
    )
    return SyncResponse(
        course_id=result.course_id,
        course_name=result.course_name,
        canvas_course_id=result.canvas_course_id,
        modules=result.modules,
        pages=result.pages,
        new_pages=result.new_pages,
        changed_pages=result.changed_pages,
        unchanged_pages=result.unchanged_pages,
        archived_pages=result.archived_pages,
        errors=result.errors,
        message=f"Synkronisert: {result.as_message()}.",
    )
