"""Settings page contracts.

Credentials only ever leave the backend as a mask. There is no route that
returns a full API key.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.core import AIProvider
from app.services.app_settings import VALID_FILENAME_STYLES, VALID_SHARING


class SecretStatus(BaseModel):
    key: str
    label: str
    configured: bool
    masked: str = ""
    source: str = "default"
    locked: bool = False  # comes from a real environment variable → GUI must not edit


class SettingsStatus(BaseModel):
    secrets: list[SecretStatus] = Field(default_factory=list)
    canvas_url: str = ""
    soundcloud_client_id: str = ""
    soundcloud_redirect_uri: str = ""
    soundcloud_connected: bool = False
    soundcloud_account: str = ""
    auth_enabled: bool = False
    env_file_writable: bool = True
    preferences: dict[str, str] = Field(default_factory=dict)


class SettingsUpdate(BaseModel):
    """Everything the Settings form can change. All fields optional."""

    canvas_url: str | None = None
    canvas_token: str | None = None
    elevenlabs_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    soundcloud_client_id: str | None = None
    soundcloud_client_secret: str | None = None
    soundcloud_redirect_uri: str | None = None
    app_password: str | None = None

    ai_provider: str | None = None
    ai_model: str | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_voice_name: str | None = None
    elevenlabs_model_id: str | None = None
    voice_stability: float | None = Field(default=None, ge=0.0, le=1.0)
    voice_similarity_boost: float | None = Field(default=None, ge=0.0, le=1.0)
    voice_style: float | None = Field(default=None, ge=0.0, le=1.0)
    voice_speed: float | None = Field(default=None, ge=0.5, le=2.0)
    voice_use_speaker_boost: bool | None = None

    monthly_budget_usd: float | None = Field(default=None, ge=0.0, le=100_000.0)
    cost_confirm_threshold_usd: float | None = Field(default=None, ge=0.0, le=100_000.0)
    filename_style: str | None = None
    soundcloud_sharing: str | None = None
    soundcloud_auto_publish: bool | None = None
    canvas_auto_publish: bool | None = None

    @field_validator("ai_provider")
    @classmethod
    def _valid_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in {AIProvider.NONE, AIProvider.OPENAI, AIProvider.ANTHROPIC}:
            raise ValueError(f"Ugyldig AI-leverandør: {value}")
        return value

    @field_validator("filename_style")
    @classmethod
    def _valid_style(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in VALID_FILENAME_STYLES:
            raise ValueError(f"Ugyldig filnavnstil: {value}")
        return value

    @field_validator("soundcloud_sharing")
    @classmethod
    def _valid_sharing(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in VALID_SHARING:
            raise ValueError(f"Ugyldig delingsvalg: {value}")
        return value


class SettingsSaved(BaseModel):
    ok: bool = True
    written: list[str] = Field(default_factory=list)
    skipped_env_managed: list[str] = Field(default_factory=list)
    message: str = ""


class ConnectionTestRequest(BaseModel):
    """Values typed into the Settings form that may not be saved yet.

    "Test connection" checks exactly what the user typed. Empty fields fall back
    to the saved configuration, and nothing in here is ever persisted.
    """

    canvas_url: str = Field(default="", max_length=500)
    canvas_token: str = Field(default="", max_length=2000)
    elevenlabs_api_key: str = Field(default="", max_length=2000)
    openai_api_key: str = Field(default="", max_length=2000)
    anthropic_api_key: str = Field(default="", max_length=2000)
    soundcloud_client_id: str = Field(default="", max_length=500)
    soundcloud_client_secret: str = Field(default="", max_length=2000)
