"""Settings: read masked credential status, save changes, test each provider."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import SECRET_KEYS, get_settings, get_store, mask, reset_store
from app.database import get_session
from app.logging_config import get_logger
from app.schemas.common import TestResult
from app.schemas.settings import (
    ConnectionTestRequest,
    SecretStatus,
    SettingsSaved,
    SettingsStatus,
    SettingsUpdate,
)
from app.security import require_auth
from app.services import (
    anthropic_service,
    canvas_service,
    elevenlabs_service,
    openai_service,
    soundcloud_service,
)
from app.services.app_settings import DEFAULTS, all_values, set_many
from app.services.errors import ServiceError

logger = get_logger("app.routes.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_auth)])

SECRET_LABELS: dict[str, str] = {
    "CANVAS_TOKEN": "Canvas-token",
    "ELEVENLABS_API_KEY": "ElevenLabs API-nøkkel",
    "OPENAI_API_KEY": "OpenAI API-nøkkel",
    "ANTHROPIC_API_KEY": "Anthropic API-nøkkel",
    "SOUNDCLOUD_CLIENT_SECRET": "SoundCloud client secret",
    "APP_PASSWORD": "Applikasjonspassord",
    "SECRET_KEY": "Signeringsnøkkel (SECRET_KEY)",
}

#: Maps the flat settings payload onto configuration keys.
SECRET_FIELD_MAP: dict[str, str] = {
    "canvas_token": "CANVAS_TOKEN",
    "elevenlabs_api_key": "ELEVENLABS_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "soundcloud_client_secret": "SOUNDCLOUD_CLIENT_SECRET",
    "app_password": "APP_PASSWORD",
}
PLAIN_FIELD_MAP: dict[str, str] = {
    "canvas_url": "CANVAS_URL",
    "soundcloud_client_id": "SOUNDCLOUD_CLIENT_ID",
    "soundcloud_redirect_uri": "SOUNDCLOUD_REDIRECT_URI",
}


def build_status(session: Session) -> SettingsStatus:
    store = get_store()
    settings = get_settings()
    secrets = [
        SecretStatus(
            key=key,
            label=SECRET_LABELS.get(key, key),
            configured=bool(store.get(key)),
            masked=mask(store.get(key)),
            source=store.source(key),
            locked=store.is_locked(key),
        )
        for key in SECRET_KEYS
    ]
    preferences = {k: v for k, v in all_values(session).items() if k in DEFAULTS}
    return SettingsStatus(
        secrets=secrets,
        canvas_url=settings.canvas_url,
        soundcloud_client_id=settings.soundcloud_client_id,
        soundcloud_redirect_uri=settings.soundcloud_redirect_uri,
        soundcloud_connected=soundcloud_service.is_connected(session),
        soundcloud_account=soundcloud_service.stored_account_name(session),
        auth_enabled=settings.auth_enabled,
        env_file_writable=_env_writable(),
        preferences=preferences,
    )


def _env_writable() -> bool:
    store = get_store()
    try:
        store.env_path.parent.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:  # pragma: no cover - read-only filesystem
        return False


@router.get("/status", response_model=SettingsStatus)
def settings_status(session: Session = Depends(get_session)) -> SettingsStatus:
    """Never returns a full secret — only whether it is set and a short mask."""
    return build_status(session)


@router.post("", response_model=SettingsSaved)
def save_settings(
    payload: SettingsUpdate, session: Session = Depends(get_session)
) -> SettingsSaved:
    data = payload.model_dump(exclude_none=True)

    env_updates: dict[str, str] = {}
    for field, key in {**SECRET_FIELD_MAP, **PLAIN_FIELD_MAP}.items():
        if field in data:
            env_updates[key] = str(data.pop(field)).strip()

    written: list[str] = []
    skipped: list[str] = []
    if env_updates:
        written, skipped = get_store().update(env_updates)
        reset_store()

    preferences = {k: v for k, v in data.items() if k in DEFAULTS}
    if preferences:
        set_many(session, preferences)
        session.commit()

    message = "Innstillingene er lagret."
    if skipped:
        message += (
            " Disse styres av miljøvariabler og ble ikke endret: "
            + ", ".join(sorted(skipped))
            + "."
        )
    logger.info("Settings updated keys=%s skipped=%s", sorted(written), sorted(skipped))
    return SettingsSaved(
        written=sorted(written), skipped_env_managed=sorted(skipped), message=message
    )


#: Which form fields belong to which provider's "Test connection" button.
TEST_FIELDS: dict[str, tuple[str, ...]] = {
    "canvas": ("canvas_url", "canvas_token"),
    "elevenlabs": ("elevenlabs_api_key",),
    "openai": ("openai_api_key",),
    "anthropic": ("anthropic_api_key",),
    "soundcloud": ("soundcloud_client_id", "soundcloud_client_secret"),
}


@router.post("/test/{provider}", response_model=TestResult)
async def test_provider(provider: str, payload: ConnectionTestRequest | None = None) -> TestResult:
    """Verify one set of credentials. Nothing here costs money.

    Values typed into the form are tested as-is, even before they are saved;
    empty fields fall back to the saved configuration. Nothing is persisted.
    """
    typed = payload.model_dump() if payload else {}
    overrides = {
        field: typed[field].strip()
        for field in TEST_FIELDS.get(provider, ())
        if typed.get(field, "").strip()
    }
    if "canvas_url" in overrides:
        overrides["canvas_url"] = overrides["canvas_url"].rstrip("/")
    settings = replace(get_settings(), **overrides)

    try:
        result = await _run_test(provider, settings)
    except ServiceError as exc:
        return TestResult(ok=False, provider=provider, detail=exc.message)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Connection test crashed for %s", provider)
        return TestResult(ok=False, provider=provider, detail=f"Uventet feil: {exc}")

    ok = bool(result.get("ok"))
    detail = str(result.get("detail", ""))
    if ok and overrides:
        detail += " Verdiene er ikke lagret ennå – klikk «Lagre innstillinger»."
    return TestResult(ok=ok, provider=provider, detail=detail)


async def _run_test(provider: str, settings: Any) -> dict[str, Any]:
    if provider == "canvas":
        return await canvas_service.check_canvas_connection(
            settings.canvas_url, settings.canvas_token
        )
    if provider == "elevenlabs":
        return await elevenlabs_service.check_elevenlabs_connection(settings.elevenlabs_api_key)
    if provider == "openai":
        return await openai_service.check_connection(settings.openai_api_key)
    if provider == "anthropic":
        return await anthropic_service.check_connection(settings.anthropic_api_key)
    if provider == "soundcloud":
        return await soundcloud_service.check_connection(
            client_id=settings.soundcloud_client_id,
            client_secret=settings.soundcloud_client_secret,
        )
    raise HTTPException(status_code=404, detail=f"Ukjent leverandør: {provider}")
