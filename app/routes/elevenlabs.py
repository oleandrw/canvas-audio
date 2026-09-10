"""ElevenLabs discovery endpoints used by the Settings and Canvas views."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.schemas.common import TestResult
from app.security import require_auth
from app.services.elevenlabs_service import ElevenLabsClient, check_elevenlabs_connection

router = APIRouter(
    prefix="/api/elevenlabs", tags=["elevenlabs"], dependencies=[Depends(require_auth)]
)


@router.get("/voices")
async def list_voices() -> list[dict[str, object]]:
    settings = get_settings()
    async with ElevenLabsClient(settings.elevenlabs_api_key) as client:
        voices = await client.list_voices()
    return [
        {
            "voice_id": v.voice_id,
            "name": v.name,
            "category": v.category,
            "description": v.description,
            "preview_url": v.preview_url,
            "labels": v.labels or {},
        }
        for v in voices
    ]


@router.get("/models")
async def list_models() -> list[dict[str, object]]:
    settings = get_settings()
    async with ElevenLabsClient(settings.elevenlabs_api_key) as client:
        models = await client.list_models()
    return [
        {
            "model_id": m.model_id,
            "name": m.name,
            "description": m.description,
            "languages": m.languages or [],
            "max_characters": m.max_characters,
        }
        for m in models
    ]


@router.post("/test", response_model=TestResult)
async def test_key() -> TestResult:
    settings = get_settings()
    result = await check_elevenlabs_connection(settings.elevenlabs_api_key)
    return TestResult(ok=True, provider="elevenlabs", detail=str(result.get("detail", "")))
