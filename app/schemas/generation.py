"""Generation job requests and status."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.core import AIProvider

VALID_PROVIDERS = {AIProvider.NONE, AIProvider.OPENAI, AIProvider.ANTHROPIC}

#: Selection shortcuts offered in the GUI.
SELECTION_MODES = ("selected", "missing", "changed", "all")


class VoiceSettings(BaseModel):
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(default=0.75, ge=0.0, le=1.0)
    style: float = Field(default=0.0, ge=0.0, le=1.0)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    use_speaker_boost: bool = True


class GenerationRequest(BaseModel):
    page_ids: list[int] = Field(default_factory=list)
    mode: str = "selected"
    ai_provider: str = AIProvider.NONE
    ai_model: str = ""
    voice_id: str = ""
    voice_name: str = ""
    tts_model: str = ""
    voice_settings: VoiceSettings | None = None
    force_regenerate: bool = False
    #: Set by the GUI after the user acknowledges a cost above the threshold.
    confirmed: bool = False

    @field_validator("ai_provider")
    @classmethod
    def _valid_provider(cls, value: str) -> str:
        if value not in VALID_PROVIDERS:
            raise ValueError(f"Ugyldig AI-leverandør: {value}")
        return value

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, value: str) -> str:
        if value not in SELECTION_MODES:
            raise ValueError(f"Ugyldig utvalgsmodus: {value}")
        return value


class JobItemOut(BaseModel):
    id: int
    page_id: int
    page_title: str
    module_name: str = ""
    status: str
    status_label: str
    message: str = ""
    characters: int = 0
    finished_at: datetime | None = None


class JobOut(BaseModel):
    id: int
    status: str
    status_label: str
    ai_provider: str
    ai_model: str = ""
    voice_name: str = ""
    tts_model: str = ""
    total_items: int
    completed_items: int
    failed_items: int
    skipped_items: int
    processed_items: int
    progress_percent: int
    estimated_cost_usd: float = 0.0
    error: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    items: list[JobItemOut] = Field(default_factory=list)


class GenerationAccepted(BaseModel):
    ok: bool = True
    job_id: int
    total_items: int
    estimated_cost_usd: float
    message: str = ""


class ConfirmationRequired(BaseModel):
    ok: bool = False
    requires_confirmation: bool = True
    reason: str
    estimated_cost_usd: float
    threshold_usd: float
    budget_usd: float = 0.0
    spent_this_month_usd: float = 0.0
    message: str
