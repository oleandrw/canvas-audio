"""Canvas player: design, preview and publishing runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.canvas_player import ICONS, STYLES, is_hex_color


class PlayerDesignIn(BaseModel):
    style: str = "kort"
    color: str = "#0b5bd3"
    label: str = Field(default="Lytt til denne siden", max_length=80)
    icon: str = Field(default="🎧", max_length=8)
    folder: str = Field(default="Lydfiler", max_length=100)

    @field_validator("style")
    @classmethod
    def _valid_style(cls, value: str) -> str:
        if value not in STYLES:
            raise ValueError("Ugyldig design.")
        return value

    @field_validator("color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        if not is_hex_color(value):
            raise ValueError("Ugyldig farge. Bruk formatet #RRGGBB.")
        return value.lower()

    @field_validator("icon")
    @classmethod
    def _valid_icon(cls, value: str) -> str:
        if value not in ICONS:
            raise ValueError("Ugyldig ikon.")
        return value


class PlayerPreviewRequest(PlayerDesignIn):
    page_id: int | None = None


class PlayerPreview(BaseModel):
    html: str


class PublishRunRequest(BaseModel):
    action: Literal["publish", "remove"] = "publish"
    #: None means every module in the selected course.
    module_ids: list[int] | None = None


class PageOutcomeOut(BaseModel):
    page_id: int
    title: str
    status: str
    message: str = ""


class PublishRunOut(BaseModel):
    id: int
    action: str
    course_name: str
    total: int
    done: int
    current: str = ""
    running: bool
    error: str = ""
    summary: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    outcomes: list[PageOutcomeOut] = Field(default_factory=list)


class PublishStatus(BaseModel):
    run: PublishRunOut | None = None
