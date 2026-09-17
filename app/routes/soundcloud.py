"""SoundCloud: OAuth connect, upload tracks, publish one playlist per Canvas module.

Everything here degrades gracefully. If the integration is not configured or the
account cannot use the API, the endpoints report that clearly and the rest of
the application is unaffected. The publishing logic itself lives in
:mod:`app.services.soundcloud_publish`, shared with automatic publishing after a
generation job.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_session, run_db
from app.logging_config import get_logger
from app.models.core import SoundCloudPlaylist, SoundCloudTrack
from app.schemas.common import OkResponse
from app.schemas.soundcloud import (
    AccountPlaylist,
    ModulePlaylistLink,
    PlaylistResult,
    PlaylistSyncResponse,
    PublishRequest,
    PublishResponse,
    SoundCloudStatus,
    UploadedItem,
    UploadRequest,
    UploadResponse,
)
from app.security import new_state_token, pkce_pair, require_auth
from app.services import soundcloud_publish, soundcloud_service
from app.services.app_settings import load_preferences
from app.services.errors import ServiceError, SoundCloudNotConnected
from app.services.soundcloud_publish import PlaylistOutcome, UploadOutcome

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
# Response shaping
# --------------------------------------------------------------------------- #


def _upload_response(outcomes: list[UploadOutcome]) -> UploadResponse:
    response = UploadResponse(items=[UploadedItem(**asdict(outcome)) for outcome in outcomes])
    response.uploaded = sum(1 for o in outcomes if o.status == "uploaded")
    response.reused = sum(1 for o in outcomes if o.status == "reused")
    response.failed = sum(1 for o in outcomes if o.status == "failed")
    response.ok = response.failed == 0
    response.message = (
        f"{response.uploaded} lastet opp, {response.reused} gjenbrukt, {response.failed} feilet."
    )
    return response


def _playlist_response(outcomes: list[PlaylistOutcome]) -> PlaylistSyncResponse:
    response = PlaylistSyncResponse(
        results=[PlaylistResult(**asdict(outcome)) for outcome in outcomes]
    )
    response.created = sum(1 for o in outcomes if o.status == "created")
    response.updated = sum(1 for o in outcomes if o.status == "updated")
    response.unchanged = sum(1 for o in outcomes if o.status == "unchanged")
    response.failed = sum(1 for o in outcomes if o.status == "failed")
    response.ok = response.failed == 0
    response.message = (
        f"{response.created} opprettet, {response.updated} oppdatert, "
        f"{response.unchanged} uendret, {response.failed} feilet."
    )
    return response


# --------------------------------------------------------------------------- #
# Upload and playlists
# --------------------------------------------------------------------------- #


@router.post("/upload", response_model=UploadResponse)
async def upload(payload: UploadRequest) -> UploadResponse:
    """Upload generated audio. A file that is already on SoundCloud is reused."""
    page_ids = payload.page_ids or None
    count = await run_db(
        lambda session: soundcloud_publish.count_uploadable(session, page_ids=page_ids)
    )
    if not count:
        return UploadResponse(ok=False, message="Ingen genererte lydfiler å laste opp.")

    async with soundcloud_publish.connected_client() as client:
        outcomes = await soundcloud_publish.upload_pages(
            client, page_ids=page_ids, force=payload.force
        )
    return _upload_response(outcomes)


@router.post("/sync-playlists", response_model=PlaylistSyncResponse)
async def sync_playlists() -> PlaylistSyncResponse:
    """One playlist per module. Tracks already in a playlist are never removed."""
    async with soundcloud_publish.connected_client() as client:
        outcomes = await soundcloud_publish.sync_playlists(client)
    if not outcomes:
        return PlaylistSyncResponse(ok=False, message="Ingen moduler å synkronisere.")
    return _playlist_response(outcomes)


@router.post("/publish", response_model=PublishResponse)
async def publish(payload: PublishRequest) -> PublishResponse:
    """Upload the modules' audio and create or update their playlists, right now.

    Unlike automatic publishing this does not wait for a module to be complete:
    the user asked for it explicitly.
    """
    module_ids = payload.module_ids or None
    async with soundcloud_publish.connected_client() as client:
        uploads, playlists = await soundcloud_publish.publish_modules(client, module_ids)
    ok, message = soundcloud_publish.summarize(uploads, playlists)
    return PublishResponse(
        ok=ok,
        message=message,
        uploads=_upload_response(uploads),
        playlists=_playlist_response(playlists),
    )


@router.get("/playlists", response_model=list[AccountPlaylist])
async def account_playlists() -> list[AccountPlaylist]:
    """Playlists on the connected SoundCloud account, for linking to a module."""
    async with soundcloud_publish.connected_client() as client:
        playlists = await client.list_my_playlists()
    return [
        AccountPlaylist(
            playlist_id=p.playlist_id,
            title=p.title,
            permalink_url=p.permalink_url,
            sharing=p.sharing,
        )
        for p in playlists
    ]


@router.put("/modules/{module_id}/playlist", response_model=OkResponse)
async def link_module_playlist(module_id: int, payload: ModulePlaylistLink) -> OkResponse:
    """Choose the playlist a module publishes to. Nothing on SoundCloud changes here."""
    if not payload.playlist_id:
        await run_db(lambda session: soundcloud_publish.link_playlist(session, module_id, None))
        return OkResponse(message="Modulen får en ny spilleliste neste gang den publiseres.")

    async with soundcloud_publish.connected_client() as client:
        info = await client.get_playlist(payload.playlist_id)
    await run_db(lambda session: soundcloud_publish.link_playlist(session, module_id, info))
    title = info.title or payload.playlist_id
    return OkResponse(
        message=f"Modulen publiserer nå til «{title}». Sporene som ligger der fra før, beholdes."
    )


@router.get("/require-connection", response_model=OkResponse)
def require_connection(session: Session = Depends(get_session)) -> OkResponse:
    if not soundcloud_service.is_connected(session):
        raise HTTPException(status_code=409, detail=SoundCloudNotConnected().message)
    return OkResponse(message="Tilkoblet.")
