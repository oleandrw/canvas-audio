"""Streaming and downloading generated audio.

The stored relative path is validated against the output directory on every
request, so a tampered path cannot read arbitrary files.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_session
from app.models.core import Page
from app.security import UnsafePathError, require_auth
from app.services.audio_service import resolve_path
from app.services.naming import slugify

router = APIRouter(prefix="/api/audio", tags=["audio"], dependencies=[Depends(require_auth)])


@router.get("/{page_id}")
def get_audio(
    page_id: int,
    download: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> FileResponse:
    page = session.get(Page, page_id)
    if page is None or page.audio is None:
        raise HTTPException(status_code=404, detail="Siden har ingen lydfil.")

    try:
        path = resolve_path(page.audio.relative_path)
    except (UnsafePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Ugyldig filsti.") from exc

    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Lydfilen er borte fra disk. Generer den på nytt.",
        )

    if download:
        # Starlette handles the RFC 5987 encoding; keep the name ASCII anyway so
        # the plain `filename=` fallback is also correct in older browsers.
        nice_name = slugify(page.title, fallback="lydfil") + ".mp3"
        return FileResponse(path, media_type="audio/mpeg", filename=nice_name)

    return FileResponse(path, media_type="audio/mpeg")
