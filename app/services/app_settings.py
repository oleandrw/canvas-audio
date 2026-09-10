"""GUI-editable, non-secret preferences stored in the database.

Credentials never come through here — they live in the environment or `.env`
(see :mod:`app.config`). This is for things like the selected course, the chosen
voice, the monthly budget and the confirmation threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import AIProvider, AppSetting

# key -> default value (as string, because that is how it is stored)
DEFAULTS: dict[str, str] = {
    "selected_course_id": "",
    "ai_provider": AIProvider.NONE,
    "ai_model": "",
    "elevenlabs_voice_id": "",
    "elevenlabs_voice_name": "",
    "elevenlabs_model_id": "",
    "voice_stability": "0.5",
    "voice_similarity_boost": "0.75",
    "voice_style": "0.0",
    "voice_speed": "1.0",
    "voice_use_speaker_boost": "1",
    "monthly_budget_usd": "25",
    "cost_confirm_threshold_usd": "5",
    "filename_style": "slug",
    "soundcloud_sharing": "private",
}

VALID_FILENAME_STYLES = ("slug", "title")
VALID_SHARING = ("private", "public")


def get_value(session: Session, key: str) -> str:
    row = session.get(AppSetting, key)
    if row is not None and row.value != "":
        return row.value
    return DEFAULTS.get(key, "")


def set_value(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    session.flush()


def set_many(session: Session, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if key in DEFAULTS:
            set_value(session, key, "" if value is None else str(value))


def get_float(session: Session, key: str, fallback: float = 0.0) -> float:
    try:
        return float(get_value(session, key))
    except (TypeError, ValueError):
        return fallback


def get_int(session: Session, key: str, fallback: int = 0) -> int:
    try:
        return int(float(get_value(session, key)))
    except (TypeError, ValueError):
        return fallback


def get_bool(session: Session, key: str) -> bool:
    return get_value(session, key) in {"1", "true", "True", "on", "yes"}


@dataclass(slots=True)
class Preferences:
    """A snapshot of everything the GUI and the job pipeline need."""

    selected_course_id: int | None
    ai_provider: str
    ai_model: str
    voice_id: str
    voice_name: str
    tts_model_id: str
    voice_settings: dict[str, Any]
    monthly_budget_usd: float
    cost_confirm_threshold_usd: float
    filename_style: str
    soundcloud_sharing: str

    @property
    def tts_ready(self) -> bool:
        """The GUI blocks generation until a voice *and* a model are chosen."""
        return bool(self.voice_id and self.tts_model_id)


def load_preferences(session: Session) -> Preferences:
    course_raw = get_value(session, "selected_course_id")
    filename_style = get_value(session, "filename_style")
    sharing = get_value(session, "soundcloud_sharing")
    return Preferences(
        selected_course_id=int(course_raw) if course_raw.isdigit() else None,
        ai_provider=get_value(session, "ai_provider") or AIProvider.NONE,
        ai_model=get_value(session, "ai_model"),
        voice_id=get_value(session, "elevenlabs_voice_id"),
        voice_name=get_value(session, "elevenlabs_voice_name"),
        tts_model_id=get_value(session, "elevenlabs_model_id"),
        voice_settings={
            "stability": get_float(session, "voice_stability", 0.5),
            "similarity_boost": get_float(session, "voice_similarity_boost", 0.75),
            "style": get_float(session, "voice_style", 0.0),
            "speed": get_float(session, "voice_speed", 1.0),
            "use_speaker_boost": get_bool(session, "voice_use_speaker_boost"),
        },
        monthly_budget_usd=get_float(session, "monthly_budget_usd", 25.0),
        cost_confirm_threshold_usd=get_float(session, "cost_confirm_threshold_usd", 5.0),
        filename_style=filename_style if filename_style in VALID_FILENAME_STYLES else "slug",
        soundcloud_sharing=sharing if sharing in VALID_SHARING else "private",
    )


def all_values(session: Session) -> dict[str, str]:
    stored = {row.key: row.value for row in session.execute(select(AppSetting)).scalars().all()}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if v != ""}}
