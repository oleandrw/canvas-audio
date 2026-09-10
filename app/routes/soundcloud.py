"""SoundCloud: OAuth connect, upload tracks, sync one playlist per Canvas module.

Everything here degrades gracefully. If the integration is not configured or the
account cannot use the API, the endpoints report that clearly and the rest of
the application is unaffected.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session, run_db
from app.logging_config import get_logger
from app.models.core import Module, Page, SoundCloudPlaylist, SoundCloudTrack
from app.schemas.common import OkResponse
from app.schemas.soundcloud import (
    PlaylistResult,
    PlaylistSyncResponse,
    SoundCloudStatus,
    UploadedItem,
    UploadRequest,
    UploadResponse,
)
from app.security import new_state_token, pkce_pair, require_auth
from app.services import soundcloud_service
from app.services.app_settings import load_preferences
from app.services.audio_service import resolve_path
from app.services.errors import ServiceError, SoundCloudNotConnected
from app.services.presenters import selected_course

logger = get_logger("app.routes.soundcloud")

router = APIRouter(
    prefix="/api/soundcloud", tags=["soundcloud"], dependencies=[Depends(require_auth)]
)

SESSION_VERIFIER_KEY = "sc_code_verifier"
SESSION_STATE_KEY = "sc_state"


@router.get("/status", response_model=SoundCloudStatus)
def status(session: Session = Depends(get_session)) -> SoundCloudStatus:
    settings = get_settings()
    prefs = load_preferences(session)
    configured = bool(settings.soundcloud_client_id and settings.soundcloud_client_secret)
    token = soundcloud_service.load_token(session)

    track_count = int(session.scalar(select(func.count(SoundCloudTrack.id))) or 0)
    playlist_count = int(session.scalar(select(func.count(SoundCloudPlaylist.id))) or 0)

    detail = ""
    if not configured:
        detail = (
            "SoundCloud er ikke konfigurert. Integrasjonen er valgfri — resten av "
            "applikasjonen fungerer uten den."
        )
    elif token is None:
        detail = "Konfigurert, men ikke tilkoblet. Klikk «Koble til SoundCloud»."

    return SoundCloudStatus(
        configured=configured,
        connected=token is not None,
        account_name=soundcloud_service.stored_account_name(session),
        expires_at=token.expires_at if token else None,
        sharing=prefs.soundcloud_sharing,
        track_count=track_count,
        playlist_count=playlist_count,
        detail=detail,
    )


@router.post("/connect")
def connect(request: Request) -> dict[str, str]:
    """Return the SoundCloud authorisation URL (OAuth 2.1 authorization code + PKCE)."""
    settings = get_settings()
    verifier, challenge = pkce_pair()
    state = new_state_token()
    request.session[SESSION_VERIFIER_KEY] = verifier
    request.session[SESSION_STATE_KEY] = state

    url = soundcloud_service.build_authorize_url(
        client_id=settings.soundcloud_client_id,
        redirect_uri=settings.soundcloud_redirect_uri,
        state=state,
        code_challenge=challenge,
    )
    return {"authorize_url": url}


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/soundcloud?feil={error}", status_code=303)

    expected_state = request.session.pop(SESSION_STATE_KEY, None)
    verifier = request.session.pop(SESSION_VERIFIER_KEY, None)
    if not code or not verifier or not expected_state or state != expected_state:
        return RedirectResponse("/soundcloud?feil=ugyldig-tilstand", status_code=303)

    settings = get_settings()
    try:
        bundle = await soundcloud_service.exchange_code(
            client_id=settings.soundcloud_client_id,
            client_secret=settings.soundcloud_client_secret,
            redirect_uri=settings.soundcloud_redirect_uri,
            code=code,
            code_verifier=verifier,
        )
    except ServiceError as exc:
        logger.warning("SoundCloud token exchange failed: %s", exc)
        return RedirectResponse("/soundcloud?feil=token", status_code=303)

    await run_db(lambda db: soundcloud_service.save_token(db, bundle))
    try:
        async with soundcloud_service.SoundCloudClient(bundle.access_token) as client:
            me = await client.me()
        name = me.get("username") or me.get("full_name") or ""
        if name:
            await run_db(lambda db: soundcloud_service.store_account_name(db, name))
    except ServiceError:
        pass  # the token is stored; the account name is cosmetic

    return RedirectResponse("/soundcloud?tilkoblet=1", status_code=303)


@router.post("/disconnect", response_model=OkResponse)
def disconnect(session: Session = Depends(get_session)) -> OkResponse:
    soundcloud_service.clear_token(session)
    session.commit()
    return OkResponse(message="Koblet fra SoundCloud.")


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #


def _pages_for_upload(session: Session, page_ids: list[int]) -> list[Page]:
    if page_ids:
        pages = list(session.execute(select(Page).where(Page.id.in_(page_ids))).scalars().all())
    else:
        course = selected_course(session)
        if course is None:
            return []
        pages = list(
            session.execute(
                select(Page)
                .where(Page.course_id == course.id, Page.archived.is_(False))
                .order_by(Page.module_id, Page.position)
            )
            .scalars()
            .all()
        )
    return [p for p in pages if p.audio is not None]


@router.post("/upload", response_model=UploadResponse)
async def upload(payload: UploadRequest) -> UploadResponse:
    """Upload generated audio. A file that is already on SoundCloud is reused."""
    settings = get_settings()

    def _collect(session: Session) -> tuple[list[dict[str, object]], str]:
        prefs = load_preferences(session)
        rows: list[dict[str, object]] = []
        for page in _pages_for_upload(session, payload.page_ids):
            track = page.soundcloud_track
            rows.append(
                {
                    "page_id": page.id,
                    "title": page.title,
                    "module_name": page.module.name if page.module else "Uten modul",
                    "course_name": page.course.name,
                    "relative_path": page.audio.relative_path,
                    "file_hash": page.audio.file_hash,
                    "existing_track_id": track.track_id if track else "",
                    "existing_file_hash": track.file_hash if track else "",
                    "existing_url": track.permalink_url if track else "",
                }
            )
        return rows, prefs.soundcloud_sharing

    rows, sharing = await run_db(_collect)
    if not rows:
        return UploadResponse(ok=False, message="Ingen genererte lydfiler å laste opp.")

    token = await soundcloud_service.get_access_token(
        client_id=settings.soundcloud_client_id,
        client_secret=settings.soundcloud_client_secret,
    )

    response = UploadResponse()
    async with soundcloud_service.SoundCloudClient(token) as client:
        for row in rows:
            page_id = int(row["page_id"])
            title = str(row["title"])

            unchanged = row["existing_track_id"] and row["existing_file_hash"] == row["file_hash"]
            if unchanged and not payload.force:
                response.reused += 1
                response.items.append(
                    UploadedItem(
                        page_id=page_id,
                        title=title,
                        track_id=str(row["existing_track_id"]),
                        permalink_url=str(row["existing_url"]),
                        status="reused",
                        message="Allerede lastet opp med samme lydfil.",
                    )
                )
                continue

            try:
                path = resolve_path(str(row["relative_path"]))
                uploaded = await client.upload_track(
                    file_path=path,
                    title=title,
                    description=f"{row['course_name']} — {row['module_name']}",
                    sharing=sharing,
                )
            except (ServiceError, ValueError) as exc:
                response.failed += 1
                response.items.append(
                    UploadedItem(page_id=page_id, title=title, status="failed", message=str(exc))
                )
                continue

            await run_db(
                lambda db, pid=page_id, up=uploaded, fh=str(row["file_hash"]): _store_track(
                    db, pid, up, fh
                )
            )
            response.uploaded += 1
            response.items.append(
                UploadedItem(
                    page_id=page_id,
                    title=title,
                    track_id=uploaded.track_id,
                    permalink_url=uploaded.permalink_url,
                    status="uploaded",
                )
            )

    response.ok = response.failed == 0
    response.message = (
        f"{response.uploaded} lastet opp, {response.reused} gjenbrukt, {response.failed} feilet."
    )
    return response


def _store_track(session: Session, page_id: int, uploaded: object, file_hash: str) -> None:
    page = session.get(Page, page_id)
    if page is None:
        return
    track = page.soundcloud_track or SoundCloudTrack(page_id=page_id)
    track.track_id = uploaded.track_id  # type: ignore[attr-defined]
    track.title = uploaded.title  # type: ignore[attr-defined]
    track.permalink_url = uploaded.permalink_url  # type: ignore[attr-defined]
    track.file_hash = file_hash
    if page.soundcloud_track is None:
        session.add(track)
    session.flush()


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #


@router.post("/sync-playlists", response_model=PlaylistSyncResponse)
async def sync_playlists() -> PlaylistSyncResponse:
    """One SoundCloud playlist per Canvas module, in Canvas page order."""
    settings = get_settings()

    def _collect(session: Session) -> list[dict[str, object]]:
        course = selected_course(session)
        if course is None:
            return []
        modules = (
            session.execute(
                select(Module).where(Module.course_id == course.id).order_by(Module.position)
            )
            .scalars()
            .all()
        )
        rows: list[dict[str, object]] = []
        for module in modules:
            pages = sorted(
                (p for p in module.pages if not p.archived), key=lambda p: (p.position, p.title)
            )
            track_ids = [
                p.soundcloud_track.track_id for p in pages if p.soundcloud_track is not None
            ]
            rows.append(
                {
                    "module_id": module.id,
                    "module_name": module.name,
                    "course_name": course.name,
                    "track_ids": track_ids,
                    "playlist_id": module.playlist.playlist_id if module.playlist else "",
                }
            )
        return rows

    modules = await run_db(_collect)
    if not modules:
        return PlaylistSyncResponse(ok=False, message="Ingen moduler å synkronisere.")

    token = await soundcloud_service.get_access_token(
        client_id=settings.soundcloud_client_id,
        client_secret=settings.soundcloud_client_secret,
    )

    response = PlaylistSyncResponse()
    async with soundcloud_service.SoundCloudClient(token) as client:
        for row in modules:
            module_id = int(row["module_id"])
            name = str(row["module_name"])
            track_ids = list(row["track_ids"])  # type: ignore[arg-type]

            if not track_ids:
                response.results.append(
                    PlaylistResult(
                        module_id=module_id,
                        module_name=name,
                        status="skipped",
                        message="Ingen opplastede spor i denne modulen.",
                    )
                )
                continue

            try:
                if row["playlist_id"]:
                    info = await client.update_playlist(
                        str(row["playlist_id"]), track_ids=track_ids
                    )
                    status_name = "updated"
                    response.updated += 1
                else:
                    info = await client.create_playlist(
                        title=name,
                        description=str(row["course_name"]),
                        track_ids=track_ids,
                    )
                    status_name = "created"
                    response.created += 1
            except ServiceError as exc:
                response.failed += 1
                response.results.append(
                    PlaylistResult(
                        module_id=module_id, module_name=name, status="failed", message=str(exc)
                    )
                )
                continue

            await run_db(
                lambda db, mid=module_id, pl=info, title=name: _store_playlist(db, mid, pl, title)
            )
            response.results.append(
                PlaylistResult(
                    module_id=module_id,
                    module_name=name,
                    playlist_id=info.playlist_id,
                    permalink_url=info.permalink_url,
                    track_count=len(track_ids),
                    status=status_name,
                )
            )

    response.ok = response.failed == 0
    response.message = (
        f"{response.created} opprettet, {response.updated} oppdatert, {response.failed} feilet."
    )
    return response


def _store_playlist(session: Session, module_id: int, info: object, title: str) -> None:
    module = session.get(Module, module_id)
    if module is None:
        return
    playlist = module.playlist or SoundCloudPlaylist(module_id=module_id)
    playlist.playlist_id = info.playlist_id or playlist.playlist_id  # type: ignore[attr-defined]
    playlist.title = info.title or title  # type: ignore[attr-defined]
    playlist.permalink_url = info.permalink_url or playlist.permalink_url  # type: ignore[attr-defined]
    playlist.track_count = info.track_count  # type: ignore[attr-defined]
    if module.playlist is None:
        session.add(playlist)
    session.flush()


@router.get("/require-connection", response_model=OkResponse)
def require_connection(session: Session = Depends(get_session)) -> OkResponse:
    if not soundcloud_service.is_connected(session):
        raise HTTPException(status_code=409, detail=SoundCloudNotConnected().message)
    return OkResponse(message="Tilkoblet.")
