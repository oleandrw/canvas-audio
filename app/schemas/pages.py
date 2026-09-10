"""Page detail and text editing."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AudioInfo(BaseModel):
    relative_path: str
    size_bytes: int
    duration_seconds: float
    duration_label: str
    voice_name: str = ""
    voice_id: str = ""
    model_id: str = ""
    character_count: int = 0
    generated_at: datetime | None = None
    exists_on_disk: bool = True


class SoundCloudInfo(BaseModel):
    track_id: str = ""
    permalink_url: str = ""
    uploaded_at: datetime | None = None
    up_to_date: bool = False


class PageDetail(BaseModel):
    id: int
    title: str
    canvas_page_id: str
    canvas_url: str
    module_id: int | None = None
    module_name: str = ""
    position: int = 0
    canvas_updated_at: datetime | None = None
    archived: bool = False

    original_text: str = ""
    processed_text: str = ""
    processed_by: str = "none"
    processed_model: str = ""
    manually_edited: bool = False

    characters: int = 0
    words: int = 0
    status: str = ""
    status_label: str = ""
    last_error: str = ""

    audio: AudioInfo | None = None
    soundcloud: SoundCloudInfo | None = None


class PageTextUpdate(BaseModel):
    """Manual edit of the text that will be sent to text-to-speech."""

    processed_text: str = Field(max_length=1_000_000)


class PageProcessRequest(BaseModel):
    ai_provider: str = "none"
    ai_model: str = ""
    force: bool = False


class PageGenerateRequest(BaseModel):
    force: bool = True
