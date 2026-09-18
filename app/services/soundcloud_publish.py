"""Publishing generated audio to SoundCloud: upload tracks, keep module playlists.

Three rules shape this module:

* A playlist is never shortened. Tracks already in it stay where they are; the
  app only adds the module's own tracks, in Canvas order, and puts a re-uploaded
  track in place of its older version.
* After a generation job, a module is published automatically only when the user
  has switched that on and every page with text has up-to-date audio.
* Automatic publishing is a follow-up to the job. It never raises, so a
  SoundCloud problem can never fail or stall audio generation.

What the API documents (checked September 2026): listing the user's playlists
with ``GET /me/playlists`` and ``next_href`` pagination, and updating ``tracks``
with ``PUT /playlists/{playlist_urn}``. It does not say whether that PUT replaces or
appends. The app therefore always sends the complete merged list, compares the
playlist SoundCloud returns with what it sent, and refuses to touch a playlist
whose full track list SoundCloud does not report.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import run_db
from app.logging_config import get_logger
from app.models.core import GenerationJob, Module, Page, SoundCloudPlaylist, SoundCloudTrack
from app.services import soundcloud_service
from app.services.app_settings import get_value, load_preferences, set_value
from app.services.audio_service import resolve_path
from app.services.errors import NotFoundError, ServiceError, UpstreamError
from app.services.presenters import selected_course
from app.services.selection import complete_module_ids
from app.services.soundcloud_service import PlaylistInfo, SoundCloudClient, UploadedTrack

logger = get_logger("app.services.soundcloud_publish")

LAST_PUBLISH_KEY = "soundcloud_last_publish"


@dataclass(slots=True)
class UploadOutcome:
    page_id: int
    title: str
    status: str  # uploaded | reused | failed
    track_id: str = ""
    permalink_url: str = ""
    message: str = ""


@dataclass(slots=True)
class PlaylistOutcome:
    module_id: int
    module_name: str
    status: str  # created | updated | unchanged | skipped | failed
    playlist_id: str = ""
    permalink_url: str = ""
    track_count: int = 0
    message: str = ""


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def merge_track_ids(
    current: list[str], wanted: list[str], replaced: dict[str, str] | None = None
) -> list[str]:
    """Combine a playlist's tracks with a module's tracks without removing anything.

    * Every track already in the playlist stays in its place, duplicates included.
    * An older upload of a page is swapped for its current track in the same place.
    * A module track that is missing is inserted next to its nearest Canvas
      neighbour already in the list, so the module keeps Canvas order even when a
      page is added later. With no neighbour present it goes at the end.
    """
    replaced = replaced or {}
    result: list[str] = []
    for track_id in current:
        new_id = replaced.get(track_id)
        if new_id is None:
            result.append(track_id)
        elif new_id not in current and new_id not in result:
            result.append(new_id)
        # Otherwise the replacement is already listed and the old version is dropped.

    for index, track_id in enumerate(wanted):
        if track_id in result:
            continue
        before = next((w for w in reversed(wanted[:index]) if w in result), None)
        if before is not None:
            result.insert(result.index(before) + 1, track_id)
            continue
        after = next((w for w in wanted[index + 1 :] if w in result), None)
        if after is not None:
            result.insert(result.index(after), track_id)
            continue
        result.append(track_id)
    return result


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def connected_client(
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[SoundCloudClient]:
    """A client with a usable access token. Raises SoundCloudNotConnected if there is none."""
    settings = get_settings()
    token = await soundcloud_service.get_access_token(
        client_id=settings.soundcloud_client_id,
        client_secret=settings.soundcloud_client_secret,
        transport=transport,
    )
    async with SoundCloudClient(token, transport=transport) as client:
        yield client


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #


def _select_pages(
    session: Session, *, page_ids: list[int] | None, module_ids: list[int] | None
) -> list[Page]:
    query = select(Page).where(Page.archived.is_(False))
    if page_ids:
        query = query.where(Page.id.in_(page_ids))
    elif module_ids is not None:
        query = query.where(Page.module_id.in_(module_ids))
    else:
        course = selected_course(session)
        if course is None:
            return []
        query = query.where(Page.course_id == course.id)
    pages = session.execute(query.order_by(Page.module_id, Page.position)).scalars().all()
    return [page for page in pages if page.audio is not None]


def count_uploadable(
    session: Session, *, page_ids: list[int] | None = None, module_ids: list[int] | None = None
) -> int:
    return len(_select_pages(session, page_ids=page_ids, module_ids=module_ids))


def _upload_plan(
    session: Session, page_ids: list[int] | None, module_ids: list[int] | None
) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    for page in _select_pages(session, page_ids=page_ids, module_ids=module_ids):
        track = page.soundcloud_track
        module_name = page.module.name if page.module else "Uten modul"
        rows.append(
            {
                "page_id": page.id,
                "title": page.title,
                "description": f"{page.course.name} — {module_name}",
                "relative_path": page.audio.relative_path,
                "file_hash": page.audio.file_hash,
                "track_id": track.track_id if track else "",
                "track_hash": track.file_hash if track else "",
                "permalink_url": track.permalink_url if track else "",
            }
        )
    return rows, load_preferences(session).soundcloud_sharing


def store_track(session: Session, page_id: int, uploaded: UploadedTrack, file_hash: str) -> None:
    """Record an upload, remembering the page's older track ids for playlist swaps."""
    page = session.get(Page, page_id)
    if page is None:
        return
    track = page.soundcloud_track
    if track is None:
        track = SoundCloudTrack(page_id=page_id, replaced_track_ids="")
        session.add(track)
    elif track.track_id and track.track_id != uploaded.track_id:
        older = (track.replaced_track_ids or "").split()
        if track.track_id not in older:
            older.append(track.track_id)
        track.replaced_track_ids = " ".join(older)
    track.track_id = uploaded.track_id
    track.title = uploaded.title
    track.permalink_url = uploaded.permalink_url
    track.file_hash = file_hash
    track.uploaded_at = datetime.now(UTC)
    session.flush()


async def find_deleted_tracks(client: SoundCloudClient, track_ids: list[str]) -> set[str]:
    """Uploaded tracks that no longer exist on SoundCloud.

    A track deleted on SoundCloud, for example together with a playlist, has to be
    uploaded again, or a playlist silently ends up without it. The account's own
    track list finds the candidates in a few requests; each candidate is then
    confirmed by a 404 for the track itself. Any other answer counts as "still
    there", so a hiccup never leads to a duplicate upload.
    """
    if not track_ids:
        return set()
    try:
        listed = await client.list_my_track_ids()
    except ServiceError as exc:
        logger.warning("Could not list SoundCloud tracks, checking each one: %s", exc)
        listed = set()
    deleted: set[str] = set()
    for track_id in dict.fromkeys(track_ids):
        if track_id in listed:
            continue
        try:
            if not await client.track_exists(track_id):
                deleted.add(track_id)
        except ServiceError as exc:
            logger.warning("Could not check SoundCloud track %s, keeping it: %s", track_id, exc)
    return deleted


async def upload_pages(
    client: SoundCloudClient,
    *,
    page_ids: list[int] | None = None,
    module_ids: list[int] | None = None,
    force: bool = False,
) -> list[UploadOutcome]:
    """Upload generated audio. An unchanged file that is still on SoundCloud is reused."""
    rows, sharing = await run_db(lambda session: _upload_plan(session, page_ids, module_ids))
    reusable = [
        row
        for row in rows
        if row["track_id"] and row["track_hash"] == row["file_hash"] and not force
    ]
    deleted = await find_deleted_tracks(client, [row["track_id"] for row in reusable])
    reuse_pages = {row["page_id"] for row in reusable if row["track_id"] not in deleted}

    outcomes: list[UploadOutcome] = []
    for row in rows:
        if row["page_id"] in reuse_pages:
            outcomes.append(
                UploadOutcome(
                    page_id=row["page_id"],
                    title=row["title"],
                    status="reused",
                    track_id=row["track_id"],
                    permalink_url=row["permalink_url"],
                    message="Allerede lastet opp med samme lydfil.",
                )
            )
            continue
        try:
            uploaded = await client.upload_track(
                file_path=resolve_path(row["relative_path"]),
                title=row["title"],
                description=row["description"],
                sharing=sharing,
            )
        except (ServiceError, ValueError) as exc:
            outcomes.append(
                UploadOutcome(
                    page_id=row["page_id"], title=row["title"], status="failed", message=str(exc)
                )
            )
            continue
        await run_db(
            lambda session, r=row, up=uploaded: store_track(
                session, r["page_id"], up, r["file_hash"]
            )
        )
        outcomes.append(
            UploadOutcome(
                page_id=row["page_id"],
                title=row["title"],
                status="uploaded",
                track_id=uploaded.track_id,
                permalink_url=uploaded.permalink_url,
                message=(
                    "Sporet var slettet på SoundCloud og er lastet opp på nytt."
                    if row["track_id"] in deleted
                    else ""
                ),
            )
        )
    return outcomes


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #


def _playlist_plan(
    session: Session, module_ids: list[int] | None
) -> tuple[list[dict[str, Any]], str]:
    if module_ids is None:
        course = selected_course(session)
        if course is None:
            return [], load_preferences(session).soundcloud_sharing
        query = select(Module).where(Module.course_id == course.id)
    else:
        query = select(Module).where(Module.id.in_(module_ids))

    rows: list[dict[str, Any]] = []
    for module in session.execute(query.order_by(Module.position)).scalars().all():
        pages = sorted(
            (p for p in module.pages if not p.archived), key=lambda p: (p.position, p.title)
        )
        tracks = [p.soundcloud_track for p in pages if p.soundcloud_track is not None]
        replaced = {
            old: track.track_id
            for track in tracks
            for old in (track.replaced_track_ids or "").split()
        }
        rows.append(
            {
                "module_id": module.id,
                "module_name": module.name,
                "course_name": module.course.name,
                "wanted": [track.track_id for track in tracks],
                "replaced": replaced,
                "playlist_id": module.playlist.playlist_id if module.playlist else "",
            }
        )
    return rows, load_preferences(session).soundcloud_sharing


def store_playlist(
    session: Session, module_id: int, info: PlaylistInfo, fallback_title: str
) -> None:
    module = session.get(Module, module_id)
    if module is None:
        return
    playlist = module.playlist
    if playlist is None:
        playlist = SoundCloudPlaylist(module_id=module_id, playlist_id=info.playlist_id)
        session.add(playlist)
    playlist.playlist_id = info.playlist_id or playlist.playlist_id
    playlist.title = info.title or playlist.title or fallback_title
    playlist.permalink_url = info.permalink_url or playlist.permalink_url
    playlist.track_count = info.track_count
    playlist.synced_at = datetime.now(UTC)
    session.flush()


def _outcome(
    row: dict[str, Any], status: str, info: PlaylistInfo, track_count: int, message: str = ""
) -> PlaylistOutcome:
    return PlaylistOutcome(
        module_id=row["module_id"],
        module_name=row["module_name"],
        status=status,
        playlist_id=info.playlist_id or row["playlist_id"],
        permalink_url=info.permalink_url,
        track_count=track_count,
        message=message,
    )


async def _sync_one(
    client: SoundCloudClient, row: dict[str, Any], sharing: str
) -> tuple[PlaylistOutcome, PlaylistInfo | None]:
    wanted: list[str] = row["wanted"]
    if not wanted:
        skipped = PlaylistOutcome(
            module_id=row["module_id"],
            module_name=row["module_name"],
            status="skipped",
            playlist_id=row["playlist_id"],
            message="Ingen opplastede spor i denne modulen.",
        )
        return skipped, None

    if not row["playlist_id"]:
        created = await client.create_playlist(
            title=row["module_name"],
            description=row["course_name"],
            track_ids=wanted,
            sharing=sharing,
        )
        if not created.playlist_id:
            raise UpstreamError(
                "SoundCloud returnerte ingen spilleliste-ID.", provider="soundcloud"
            )
        message, created = await _check_tracks(client, created, wanted)
        return _outcome(row, "created", created, len(wanted), message), created

    info, current = await read_playlist(client, row["playlist_id"])
    merged = merge_track_ids(current, wanted, row["replaced"])
    if merged == current:
        return _outcome(row, "unchanged", info, len(merged)), info

    updated = await client.update_playlist(row["playlist_id"], track_ids=merged)
    if not updated.playlist_id:
        updated = replace(updated, playlist_id=row["playlist_id"])
    message, updated = await _check_tracks(client, updated, merged)
    return _outcome(row, "updated", updated, len(merged), message), updated


def _is_subsequence(short: list[str], long: list[str]) -> bool:
    """True when ``short`` appears in ``long`` in the same order, gaps allowed."""
    remaining = iter(long)
    return all(any(item == other for other in remaining) for item in short)


async def read_playlist(
    client: SoundCloudClient, playlist_id: str
) -> tuple[PlaylistInfo, list[str]]:
    """The playlist and its complete track list, or an error when that is uncertain.

    SoundCloud's ``track_count`` cannot tell the size, since it only counts public
    tracks. So two views are compared before anything is written: the tracks
    embedded in the playlist and the paged ``/tracks`` list, which also includes
    blocked tracks. The paged list is used when it holds the embedded one in the
    same order; anything else is refused rather than risk dropping or reordering
    the user's tracks.
    """
    info = await client.get_playlist(playlist_id)
    paged = await client.list_playlist_track_ids(playlist_id)
    consistent = paged == info.track_ids or (
        len(paged) > len(info.track_ids) and _is_subsequence(info.track_ids, paged)
    )
    if not consistent or info.public_track_count > len(paged):
        raise UpstreamError(
            f"SoundCloud ga ikke en entydig sporliste for spillelisten ({len(info.track_ids)} "
            f"og {len(paged)} spor). Appen endrer den ikke, for ikke å fjerne eller stokke "
            "om spor.",
            provider="soundcloud",
        )
    return replace(info, track_ids=paged, track_count=len(paged)), paged


async def _check_tracks(
    client: SoundCloudClient, response: PlaylistInfo, expected: list[str]
) -> tuple[str, PlaylistInfo]:
    """Compare what SoundCloud holds with what was sent. Returns a warning, or ``""``."""
    actual = response.track_ids
    if actual != expected:
        # A response may leave tracks out; read the full list before warning.
        actual = await client.list_playlist_track_ids(response.playlist_id)
    info = replace(response, track_ids=actual, track_count=len(actual))
    if actual == expected:
        return "", info
    message = (
        f"SoundCloud viser {len(actual)} spor i spillelisten, men appen sendte "
        f"{len(expected)}. Sjekk spillelisten på SoundCloud."
    )
    return message, info


async def sync_playlists(
    client: SoundCloudClient, *, module_ids: list[int] | None = None
) -> list[PlaylistOutcome]:
    """Create or update one playlist per module. Tracks are never removed."""
    rows, sharing = await run_db(lambda session: _playlist_plan(session, module_ids))
    outcomes: list[PlaylistOutcome] = []
    for row in rows:
        try:
            outcome, info = await _sync_one(client, row, sharing)
        except NotFoundError:
            message = (
                "Spillelisten modulen er koblet til, finnes ikke lenger på SoundCloud. "
                "Velg en annen, eller «Ny spilleliste»."
            )
            outcome, info = _failed(row, message), None
        except ServiceError as exc:
            outcome, info = _failed(row, str(exc)), None
        if info is not None:
            await run_db(
                lambda session, r=row, i=info: store_playlist(
                    session, r["module_id"], i, r["module_name"]
                )
            )
        outcomes.append(outcome)
    return outcomes


def _failed(row: dict[str, Any], message: str) -> PlaylistOutcome:
    return PlaylistOutcome(
        module_id=row["module_id"],
        module_name=row["module_name"],
        status="failed",
        playlist_id=row["playlist_id"],
        message=message,
    )


def link_playlist(session: Session, module_id: int, info: PlaylistInfo | None) -> None:
    """Point a module at an existing playlist, or clear it (``None``) to get a new one."""
    module = session.get(Module, module_id)
    if module is None:
        raise NotFoundError("Fant ikke modulen.", provider="soundcloud")
    if info is None:
        if module.playlist is not None:
            session.delete(module.playlist)
            session.flush()
        return
    store_playlist(session, module_id, info, module.name)


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


async def publish_modules(
    client: SoundCloudClient, module_ids: list[int] | None
) -> tuple[list[UploadOutcome], list[PlaylistOutcome]]:
    uploads = await upload_pages(client, module_ids=module_ids)
    playlists = await sync_playlists(client, module_ids=module_ids)
    return uploads, playlists


def summarize(uploads: list[UploadOutcome], playlists: list[PlaylistOutcome]) -> tuple[bool, str]:
    def count(items: list[Any], status: str) -> int:
        return sum(1 for item in items if item.status == status)

    parts = [
        f"{count(uploads, 'uploaded')} spor lastet opp",
        f"{count(uploads, 'reused')} gjenbrukt",
    ]
    for status, label in (
        ("created", "opprettet"),
        ("updated", "oppdatert"),
        ("unchanged", "uendret"),
    ):
        number = count(playlists, status)
        if number:
            noun = "spilleliste" if number == 1 else "spillelister"
            parts.append(f"{number} {noun} {label}")

    failed_tracks = count(uploads, "failed")
    if failed_tracks:
        parts.append(f"{failed_tracks} spor feilet")
    failed_playlists = count(playlists, "failed")
    if failed_playlists:
        noun = "spilleliste" if failed_playlists == 1 else "spillelister"
        parts.append(f"{failed_playlists} {noun} feilet")

    failures = [item for item in (*uploads, *playlists) if item.status == "failed"]
    warnings = [
        p for p in playlists if p.message and p.status in ("created", "updated", "unchanged")
    ]
    message = ", ".join(parts)
    detail = (failures or warnings)[0].message if (failures or warnings) else ""
    if detail:
        message = f"{message}: {detail.rstrip(' .')}"
    return not failures and not warnings, message + "."


def record_last_publish(
    session: Session, *, job_id: int, ok: bool, message: str, modules: list[str]
) -> None:
    record = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "job_id": job_id,
        "ok": ok,
        "message": message,
        "modules": modules,
    }
    set_value(session, LAST_PUBLISH_KEY, json.dumps(record, ensure_ascii=False))


def last_publish(session: Session) -> dict[str, Any] | None:
    raw = get_value(session, LAST_PUBLISH_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        data["at_label"] = datetime.fromisoformat(data["at"]).strftime("%d.%m.%Y %H:%M")
    except (KeyError, TypeError, ValueError):
        data["at_label"] = ""
    return data


def _auto_publish_modules(session: Session, job_id: int) -> list[int]:
    if not load_preferences(session).soundcloud_auto_publish:
        return []
    if not soundcloud_service.is_connected(session):
        return []
    job = session.get(GenerationJob, job_id)
    if job is None:
        return []
    module_ids = {
        item.page.module_id
        for item in job.items
        if item.page is not None and item.page.module_id is not None
    }
    return complete_module_ids(session, sorted(module_ids))


async def publish_after_job(
    job_id: int, *, transport: httpx.AsyncBaseTransport | None = None
) -> str | None:
    """Publish the modules a finished job completed. Never raises.

    Returns the summary message, or ``None`` when there was nothing to publish.
    """
    settings = get_settings()
    if not (settings.soundcloud_client_id and settings.soundcloud_client_secret):
        return None
    try:
        module_ids = await run_db(lambda session: _auto_publish_modules(session, job_id))
        if not module_ids:
            return None
        logger.info("Auto-publishing modules %s to SoundCloud after job %s", module_ids, job_id)
        async with connected_client(transport) as client:
            uploads, playlists = await publish_modules(client, module_ids)
        ok, message = summarize(uploads, playlists)
        names = [p.module_name for p in playlists]
    except Exception as exc:  # a SoundCloud problem must never fail the generation job
        logger.exception("Automatic SoundCloud publishing failed after job %s", job_id)
        ok, message, names = False, f"Automatisk publisering til SoundCloud feilet: {exc}", []

    logger.info("SoundCloud auto-publish after job %s: %s", job_id, message)
    try:
        await run_db(
            lambda session: record_last_publish(
                session, job_id=job_id, ok=ok, message=message, modules=names
            )
        )
    except Exception:  # pragma: no cover - the record is informational only
        logger.exception("Could not store the SoundCloud publishing result")
    return message
