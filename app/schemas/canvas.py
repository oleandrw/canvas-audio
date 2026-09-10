"""Canvas-related request and response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CourseOption(BaseModel):
    id: str
    name: str
    course_code: str = ""


class SyncRequest(BaseModel):
    """Course id may be typed in by hand when the token cannot list courses."""

    course_id: str = Field(min_length=1, max_length=64)

    @field_validator("course_id")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Emne-ID kan ikke være tom.")
        return cleaned


class SyncResponse(BaseModel):
    ok: bool = True
    course_id: int
    course_name: str
    canvas_course_id: str
    modules: int
    pages: int
    new_pages: int
    changed_pages: int
    unchanged_pages: int
    archived_pages: int
    errors: list[str] = Field(default_factory=list)
    message: str = ""


class PageSummaryOut(BaseModel):
    id: int
    title: str
    position: int
    characters: int
    words: int
    status: str
    status_label: str
    module_id: int | None = None
    module_name: str = ""
    has_audio: bool = False
    archived: bool = False
    canvas_updated_at: datetime | None = None
    soundcloud_track_id: str = ""


class ModuleOut(BaseModel):
    id: int | None
    name: str
    position: int
    pages: list[PageSummaryOut] = Field(default_factory=list)


class CourseOut(BaseModel):
    id: int
    canvas_course_id: str
    name: str
    course_code: str = ""
    synced_at: datetime | None = None
    module_count: int = 0
    page_count: int = 0
    modules: list[ModuleOut] = Field(default_factory=list)
