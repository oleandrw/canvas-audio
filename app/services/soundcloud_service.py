"""SoundCloud integration — optional by design.

What the official API actually supports (verified against
https://developers.soundcloud.com/docs/api/guide, September 2026):

* **OAuth 2.1.** ``client_credentials`` only reaches public resources, so uploads
  and playlists require the *authorization code* flow, and **PKCE is required**.
  Access tokens last about an hour and refresh tokens are **single-use** — every
  refresh returns a new one that must be stored immediately.
* **Upload** — ``POST /tracks`` as multipart with ``track[title]``,
  ``track[description]``, ``track[sharing]`` and ``track[asset_data]``.
* **Playlists** — ``POST /playlists`` with a JSON ``playlist`` object containing
  a ``tracks`` array of ``{"id": N}``; update with ``PUT /playlists/{id}``,
  sending the complete track list again.

Known limitations, documented in the README and shown in the GUI:

* Registering an API application requires a **SoundCloud Artist Pro** subscription
  and approval is manual.
* There are open reports of ``POST /tracks`` returning 502/503 for some accounts.

None of this may block the Canvas → ElevenLabs part of the application, so every
entry point here fails softly with :class:`SoundCloudNotConnected`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.database import run_db
from app.logging_config import get_logger
from app.models.core import OAuthToken
from app.security import get_cipher
from app.services.errors import ConfigurationError, SoundCloudNotConnected, UpstreamError
from app.services.http import build_client, raise_for_status, with_retries

logger = get_logger("app.services.soundcloud")

PROVIDER = "soundcloud"
AUTHORIZE_URL = "https://secure.soundcloud.com/authorize"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
API_BASE = "https://api.soundcloud.com"

#: Refresh a little early so a long upload never starts with a nearly dead token.
REFRESH_MARGIN = timedelta(minutes=5)

ERROR_MESSAGES = {
    401: "SoundCloud avviste tokenet (401). Koble til på nytt.",
    403: "SoundCloud nektet handlingen (403). Kontoen mangler rettigheter (krever Artist Pro).",
    404: "Fant ikke ressursen på SoundCloud (404).",
    429: "SoundCloud begrenser antall forespørsler (429).",
}


@dataclass(slots=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: datetime | None
    scope: str = ""


@dataclass(slots=True)
class UploadedTrack:
    track_id: str
    title: str
    permalink_url: str


@dataclass(slots=True)
class PlaylistInfo:
    playlist_id: str
    title: str
    permalink_url: str
    track_count: int


# --------------------------------------------------------------------------- #
# Token storage (encrypted at rest)
# --------------------------------------------------------------------------- #


def load_token(session: Session) -> TokenBundle | None:
    row = session.get(OAuthToken, PROVIDER)
    if row is None:
        return None
    cipher = get_cipher()
    access = cipher.decrypt(row.access_token_enc)
    refresh = cipher.decrypt(row.refresh_token_enc)
    if not access and not refresh:
        return None
    return TokenBundle(
        access_token=access, refresh_token=refresh, expires_at=row.expires_at, scope=row.scope
    )


def save_token(session: Session, bundle: TokenBundle, account_name: str = "") -> None:
    cipher = get_cipher()
    row = session.get(OAuthToken, PROVIDER)
    if row is None:
        row = OAuthToken(provider=PROVIDER)
        session.add(row)
    row.access_token_enc = cipher.encrypt(bundle.access_token)
    row.refresh_token_enc = cipher.encrypt(bundle.refresh_token)
    row.expires_at = bundle.expires_at
    row.scope = bundle.scope
    if account_name:
        row.account_name = account_name
    session.flush()


def clear_token(session: Session) -> None:
    row = session.get(OAuthToken, PROVIDER)
    if row is not None:
        session.delete(row)


def stored_account_name(session: Session) -> str:
    row = session.get(OAuthToken, PROVIDER)
    return row.account_name if row else ""


def is_connected(session: Session) -> bool:
    return load_token(session) is not None


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #


def build_authorize_url(
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    if not client_id:
        raise ConfigurationError("SoundCloud client ID er ikke satt.", provider=PROVIDER)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _parse_token_response(payload: dict[str, Any]) -> TokenBundle:
    access = payload.get("access_token") or ""
    if not access:
        raise UpstreamError("SoundCloud returnerte ingen access token.", provider=PROVIDER)
    expires_in = payload.get("expires_in")
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
    return TokenBundle(
        access_token=access,
        refresh_token=payload.get("refresh_token") or "",
        expires_at=expires_at,
        scope=payload.get("scope") or "",
    )


async def _post_token(
    data: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None
) -> TokenBundle:
    async with build_client(transport=transport, timeout=60.0) as client:

        async def _call() -> httpx.Response:
            response = await client.post(
                TOKEN_URL,
                data=data,
                headers={
                    "Accept": "application/json; charset=utf-8",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            raise_for_status(response, provider=PROVIDER, messages=ERROR_MESSAGES)
            return response

        response = await with_retries(_call, provider=PROVIDER, description="SoundCloud token")
    return _parse_token_response(response.json())


async def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenBundle:
    return await _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "code": code,
        },
        transport=transport,
    )


async def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenBundle:
    """Exchange a refresh token. The returned refresh token replaces the old one."""
    if not refresh_token:
        raise SoundCloudNotConnected("Mangler refresh token — koble til SoundCloud på nytt.")
    return await _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        transport=transport,
    )


def _is_expiring(bundle: TokenBundle) -> bool:
    if bundle.expires_at is None:
        return False
    expires_at = bundle.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at - REFRESH_MARGIN <= datetime.now(UTC)


async def get_access_token(
    *,
    client_id: str,
    client_secret: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Return a usable access token, refreshing and re-persisting it if needed."""
    bundle = await run_db(load_token)
    if bundle is None:
        raise SoundCloudNotConnected()

    if not _is_expiring(bundle):
        return bundle.access_token

    logger.info("Refreshing SoundCloud access token")
    refreshed = await refresh_access_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=bundle.refresh_token,
        transport=transport,
    )
    # Single-use refresh tokens: persist immediately or the connection is lost.
    await run_db(lambda session: save_token(session, refreshed))
    return refreshed.access_token


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #


class SoundCloudClient:
    def __init__(
        self,
        access_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        if not access_token:
            raise SoundCloudNotConnected()
        self._client = build_client(
            base_url=API_BASE,
            headers={
                "Authorization": f"OAuth {access_token}",
                "Accept": "application/json; charset=utf-8",
            },
            timeout=timeout or 600.0,  # uploads can be large
            transport=transport,
        )

    async def __aenter__(self) -> SoundCloudClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def _call() -> httpx.Response:
            response = await self._client.request(method, url, **kwargs)
            raise_for_status(response, provider=PROVIDER, messages=ERROR_MESSAGES)
            return response

        return await with_retries(
            _call, provider=PROVIDER, description=f"SoundCloud {method} {url}"
        )

    async def me(self) -> dict[str, Any]:
        return (await self._request("GET", "/me")).json()

    async def upload_track(
        self,
        *,
        file_path: Path,
        title: str,
        description: str = "",
        sharing: str = "private",
        filename: str | None = None,
    ) -> UploadedTrack:
        if not file_path.is_file():
            raise UpstreamError(f"Lydfilen finnes ikke: {file_path.name}", provider=PROVIDER)

        data = {
            "track[title]": title,
            "track[description]": description,
            "track[sharing]": sharing,
        }
        with file_path.open("rb") as handle:
            files = {"track[asset_data]": (filename or file_path.name, handle, "audio/mpeg")}
            response = await self._request("POST", "/tracks", data=data, files=files)

        payload = response.json()
        track_id = str(payload.get("id") or "")
        if not track_id:
            raise UpstreamError("SoundCloud returnerte ingen track-ID.", provider=PROVIDER)
        logger.info("SoundCloud upload completed track_id=%s title=%s", track_id, title)
        return UploadedTrack(
            track_id=track_id,
            title=payload.get("title") or title,
            permalink_url=payload.get("permalink_url") or "",
        )

    async def create_playlist(
        self, *, title: str, track_ids: list[str], description: str = "", sharing: str = "private"
    ) -> PlaylistInfo:
        body = {
            "playlist": {
                "title": title,
                "description": description,
                "sharing": sharing,
                "tracks": _track_refs(track_ids),
            }
        }
        payload = (await self._request("POST", "/playlists", json=body)).json()
        return _playlist_info(payload, fallback_title=title)

    async def update_playlist(self, playlist_id: str, *, track_ids: list[str]) -> PlaylistInfo:
        """Replace the playlist's tracks. SoundCloud expects the complete list."""
        body = {"playlist": {"tracks": _track_refs(track_ids)}}
        payload = (await self._request("PUT", f"/playlists/{playlist_id}", json=body)).json()
        return _playlist_info(payload, fallback_title="")

    async def get_playlist(self, playlist_id: str) -> PlaylistInfo:
        payload = (await self._request("GET", f"/playlists/{playlist_id}")).json()
        return _playlist_info(payload, fallback_title="")


def _track_refs(track_ids: list[str]) -> list[dict[str, int]]:
    refs: list[dict[str, int]] = []
    for track_id in track_ids:
        text = str(track_id).strip()
        if text.isdigit():
            refs.append({"id": int(text)})
    return refs


def _playlist_info(payload: dict[str, Any], *, fallback_title: str) -> PlaylistInfo:
    tracks = payload.get("tracks") or []
    return PlaylistInfo(
        playlist_id=str(payload.get("id") or ""),
        title=payload.get("title") or fallback_title,
        permalink_url=payload.get("permalink_url") or "",
        track_count=len(tracks) if isinstance(tracks, list) else 0,
    )


async def check_connection(
    *,
    client_id: str,
    client_secret: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Used by the Settings page. Reports "not connected" instead of failing hard."""
    if not client_id or not client_secret:
        return {
            "ok": False,
            "detail": "SoundCloud client ID/secret mangler. Integrasjonen er valgfri.",
        }
    token = await get_access_token(
        client_id=client_id, client_secret=client_secret, transport=transport
    )
    async with SoundCloudClient(token, transport=transport) as client:
        me = await client.me()
    name = me.get("username") or me.get("full_name") or "ukjent konto"
    await run_db(lambda session: store_account_name(session, name))
    return {"ok": True, "detail": f"Tilkoblet som {name}.", "account_name": name}


def store_account_name(session: Session, name: str) -> None:
    row = session.get(OAuthToken, PROVIDER)
    if row is not None:
        row.account_name = name
