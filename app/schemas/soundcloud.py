"""SoundCloud request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class SoundCloudStatus(BaseModel):
    configured: bool = False
    connected: bool = False
    account_name: str = ""
    expires_at: datetime | None = None
    sharing: str = "private"
    track_count: int = 0
    playlist_count: int = 0
    detail: str = ""


class UploadRequest(BaseModel):
    page_ids: list[int] = Field(default_factory=list)
    force: bool = False


class UploadedItem(BaseModel):
    page_id: int
    title: str
    track_id: str = ""
    permalink_url: str = ""
    status: str = "uploaded"  # uploaded | reused | skipped | failed
    message: str = ""


class UploadResponse(BaseModel):
    ok: bool = True
    uploaded: int = 0
    reused: int = 0
    failed: int = 0
    items: list[UploadedItem] = Field(default_factory=list)
    message: str = ""


class PlaylistResult(BaseModel):
    module_id: int
    module_name: str
    playlist_id: str = ""
    permalink_url: str = ""
    track_count: int = 0
    status: str = "created"  # created | updated | unchanged | skipped | failed
    message: str = ""


class PlaylistSyncResponse(BaseModel):
    ok: bool = True
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    results: list[PlaylistResult] = Field(default_factory=list)
    message: str = ""


class AccountPlaylist(BaseModel):
    """A playlist on the connected SoundCloud account.

    No track count: SoundCloud's ``track_count`` only counts public tracks, so it
    reads 0 for a private playlist.
    """

    playlist_id: str
    title: str = ""
    permalink_url: str = ""
    sharing: str = ""


class ModulePlaylistLink(BaseModel):
    """An existing playlist id, or empty to let the module get a new playlist."""

    playlist_id: str = Field(default="", max_length=64)

    @field_validator("playlist_id")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        value = value.strip()
        if value and not value.isdigit():
            raise ValueError("Ugyldig spilleliste-ID.")
        return value


class PublishRequest(BaseModel):
    """Modules to publish now. Empty means every module of the selected course."""

    module_ids: list[int] = Field(default_factory=list)


class PublishResponse(BaseModel):
    ok: bool = True
    message: str = ""
    uploads: UploadResponse
    playlists: PlaylistSyncResponse
