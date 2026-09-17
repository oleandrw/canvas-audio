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
  a ``tracks`` array of ``{"urn": "soundcloud:tracks:N"}``; update with
  ``PUT /playlists/{playlist_urn}``, sending the complete track list again.
  Numeric ``id`` fields are deprecated because ids no longer fit in 32 bits
  (openapi/api.yaml in github.com/soundcloud/api, api#448).

Known limitations, documented in the README and shown in the GUI:

* Registering an API application requires a **SoundCloud Artist Pro** subscription
  and approval is manual.
* There are open reports of ``POST /tracks`` returning 502/503 for some accounts.

None of this may block the Canvas → ElevenLabs part of the application, so every
entry point here fails softly with :class:`SoundCloudNotConnected`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from app.services.errors import (
    ConfigurationError,
    NotFoundError,
    SoundCloudNotConnected,
    UpstreamError,
)
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
    #: Tracks listed in this response.
    track_count: int
    track_ids: list[str] = field(default_factory=list)
    sharing: str = ""
    #: SoundCloud's own ``track_count``. It only counts *public* tracks, so a private
    #: playlist reports 0 however many tracks it holds (api#210, api#253).
    public_track_count: int = 0


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
        track_id = _numeric_id(payload, "tracks")
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
        url = f"/playlists/{_playlist_urn(playlist_id)}"
        payload = (await self._request("PUT", url, json=body)).json()
        return _playlist_info(payload, fallback_title="")

    async def get_playlist(self, playlist_id: str) -> PlaylistInfo:
        url = f"/playlists/{_playlist_urn(playlist_id)}"
        payload = (await self._request("GET", url, params={"access": ALL_ACCESS})).json()
        return _playlist_info(payload, fallback_title="")

    async def list_playlist_track_ids(self, playlist_id: str, *, max_pages: int = 100) -> list[str]:
        """Every track in a playlist, in order (``GET /playlists/{urn}/tracks``), all pages.

        The tracks embedded in a playlist object are not enough to rewrite it: that
        list may be cut short, and tracks the account cannot play are left out unless
        ``access`` includes ``blocked``, so a PUT built from it would remove them. A
        track this cannot identify stops the read instead of being skipped.
        """
        track_ids: list[str] = []
        url = f"/playlists/{_playlist_urn(playlist_id)}/tracks"
        params: dict[str, Any] | None = {"access": ALL_ACCESS, "linked_partitioning": "true"}
        for _ in range(max_pages):
            payload = (await self._request("GET", url, params=params)).json()
            items = payload.get("collection") if isinstance(payload, dict) else payload
            for item in items or []:
                track_id = _numeric_id(item, "tracks") if isinstance(item, dict) else ""
                if not track_id:
                    raise UpstreamError(
                        "SoundCloud sendte et spor i spillelisten som appen ikke kan lese.",
                        provider=PROVIDER,
                    )
                track_ids.append(track_id)
            next_href = payload.get("next_href") if isinstance(payload, dict) else None
            if not next_href or not items:
                return track_ids
            # httpx replaces a URL's query string with ``params``, which would drop
            # the cursor, so ``access`` is merged into next_href itself when missing.
            next_url = httpx.URL(str(next_href))
            if "access" not in next_url.params:
                next_url = next_url.copy_merge_params({"access": ALL_ACCESS})
            url, params = str(next_url), None
        raise UpstreamError(
            "Spillelisten har flere sider med spor enn appen leser. Appen endrer den ikke.",
            provider=PROVIDER,
        )

    async def list_my_track_ids(self, *, max_pages: int = 50) -> set[str]:
        """Ids of every track on the connected account (``GET /me/tracks``), all pages.

        Unlike the ``/tracks`` search, which leaves private tracks out, this lists the
        account's own private tracks too. An empty page ends the list.
        """
        track_ids: set[str] = set()
        url = "/me/tracks"
        params: dict[str, Any] | None = {"linked_partitioning": "true", "limit": 200}
        for _ in range(max_pages):
            payload = (await self._request("GET", url, params=params)).json()
            items = payload.get("collection") if isinstance(payload, dict) else payload
            for item in items or []:
                if isinstance(item, dict) and (track_id := _numeric_id(item, "tracks")):
                    track_ids.add(track_id)
            next_href = payload.get("next_href") if isinstance(payload, dict) else None
            if not next_href or not items:
                return track_ids
            url, params = str(next_href), None
        raise UpstreamError(
            "SoundCloud-kontoen har flere sider med spor enn appen leser.", provider=PROVIDER
        )

    async def track_exists(self, track_id: str) -> bool:
        """False only when SoundCloud answers 404 for the track. Other errors raise."""
        try:
            await self._request("GET", f"/tracks/soundcloud:tracks:{track_id}")
        except NotFoundError:
            return False
        return True

    async def list_my_playlists(self, *, max_pages: int = 20) -> list[PlaylistInfo]:
        """The authenticated user's playlists (``GET /me/playlists``), all pages.

        Documented pagination: ``linked_partitioning=true`` returns a
        ``collection`` and a ``next_href`` that is absent on the last page. An
        empty page can still carry a ``next_href`` (api#485), so it ends the list.
        """
        playlists: list[PlaylistInfo] = []
        url = "/me/playlists"
        params: dict[str, Any] | None = {
            "show_tracks": "false",
            "linked_partitioning": "true",
            "limit": 50,
        }
        for _ in range(max_pages):
            payload = (await self._request("GET", url, params=params)).json()
            items = payload.get("collection") if isinstance(payload, dict) else payload
            for item in items or []:
                if isinstance(item, dict) and _numeric_id(item, "playlists"):
                    playlists.append(_playlist_info(item, fallback_title=""))
            next_href = payload.get("next_href") if isinstance(payload, dict) else None
            if not next_href or not items:
                break
            url, params = str(next_href), None
        return playlists


#: Ask for every track, including ones the account cannot play. The default,
#: ``playable,preview``, leaves blocked tracks out of a playlist's track list.
ALL_ACCESS = "playable,preview,blocked"

# SoundCloud ids no longer fit in 32 bits, so the API deprecated the numeric ``id``
# for string URNs such as ``soundcloud:tracks:123``. A playlist body that lists
# tracks as ``{"id": 2401076190}`` is rejected with "Could not parse JSON request
# body". Ids are stored as plain digits and turned into URNs at the API boundary.
# https://developers.soundcloud.com/blog/urn-num-to-string/


def _numeric_id(payload: dict[str, Any], kind: str) -> str:
    """Digits of a ``soundcloud:<kind>:<n>`` URN, falling back to the old ``id``."""
    urn = payload.get("urn")
    prefix = f"soundcloud:{kind}:"
    if isinstance(urn, str) and urn.startswith(prefix) and urn[len(prefix) :].isdigit():
        return urn[len(prefix) :]
    legacy = str(payload.get("id") or "")
    return legacy if legacy.isdigit() else ""


def _playlist_urn(playlist_id: str) -> str:
    text = str(playlist_id).strip()
    return text if text.startswith("soundcloud:playlists:") else f"soundcloud:playlists:{text}"


def _track_refs(track_ids: list[str]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for track_id in track_ids:
        text = str(track_id).strip()
        if text.isdigit():
            refs.append({"urn": f"soundcloud:tracks:{text}"})
    return refs


def _playlist_info(payload: dict[str, Any], *, fallback_title: str) -> PlaylistInfo:
    tracks = payload.get("tracks")
    track_ids = [
        track_id
        for track in (tracks if isinstance(tracks, list) else [])
        if isinstance(track, dict) and (track_id := _numeric_id(track, "tracks"))
    ]
    public_count = payload.get("track_count")
    return PlaylistInfo(
        playlist_id=_numeric_id(payload, "playlists"),
        title=payload.get("title") or fallback_title,
        permalink_url=payload.get("permalink_url") or "",
        track_count=len(track_ids),
        track_ids=track_ids,
        sharing=str(payload.get("sharing") or ""),
        public_track_count=public_count if isinstance(public_count, int) else 0,
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
