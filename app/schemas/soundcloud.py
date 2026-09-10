"""SoundCloud request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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
    status: str = "created"  # created | updated | skipped | failed
    message: str = ""


class PlaylistSyncResponse(BaseModel):
    ok: bool = True
    created: int = 0
    updated: int = 0
    failed: int = 0
    results: list[PlaylistResult] = Field(default_factory=list)
    message: str = ""
