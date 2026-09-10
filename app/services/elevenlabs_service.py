"""ElevenLabs text-to-speech client.

Voices and models are always discovered from the API — nothing is hardcoded, and
the GUI refuses to start a job until the user has picked both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.logging_config import get_logger
from app.services.errors import ConfigurationError, UpstreamError
from app.services.http import build_client, raise_for_status, with_retries
from app.services.text_service import ensure_plain_text

logger = get_logger("app.services.elevenlabs")

PROVIDER = "elevenlabs"
BASE_URL = "https://api.elevenlabs.io"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

ERROR_MESSAGES = {
    401: "ElevenLabs avviste API-nøkkelen (401).",
    402: "ElevenLabs-kvoten er brukt opp (402). Fyll på abonnementet for å fortsette.",
    422: "ElevenLabs avviste forespørselen (422). Sjekk valgt voice og modell.",
    429: "ElevenLabs begrenser antall forespørsler (429).",
}


@dataclass(slots=True)
class Voice:
    voice_id: str
    name: str
    category: str = ""
    description: str = ""
    preview_url: str = ""
    labels: dict[str, str] | None = None


@dataclass(slots=True)
class TTSModel:
    model_id: str
    name: str
    description: str = ""
    languages: list[str] | None = None
    max_characters: int = 0


@dataclass(slots=True)
class Subscription:
    tier: str
    character_count: int
    character_limit: int

    @property
    def remaining(self) -> int:
        return max(self.character_limit - self.character_count, 0)


@dataclass(slots=True)
class SynthesisResult:
    audio: bytes
    characters: int
    voice_id: str
    model_id: str


class ElevenLabsClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        if not api_key:
            raise ConfigurationError("ElevenLabs API-nøkkel er ikke satt.", provider=PROVIDER)
        self._client = build_client(
            base_url=BASE_URL,
            headers={"xi-api-key": api_key, "Accept": "application/json"},
            timeout=timeout or 180.0,  # synthesis of a long page takes a while
            transport=transport,
        )

    async def __aenter__(self) -> ElevenLabsClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        async def _call() -> httpx.Response:
            response = await self._client.request(method, url, params=params, json=json)
            raise_for_status(response, provider=PROVIDER, messages=ERROR_MESSAGES)
            return response

        return await with_retries(
            _call, provider=PROVIDER, description=f"ElevenLabs {method} {url}"
        )

    # -- discovery -------------------------------------------------------

    async def get_subscription(self) -> Subscription:
        data = (await self._request("GET", "/v1/user/subscription")).json()
        return Subscription(
            tier=str(data.get("tier", "ukjent")),
            character_count=int(data.get("character_count") or 0),
            character_limit=int(data.get("character_limit") or 0),
        )

    async def list_voices(self) -> list[Voice]:
        voices: list[Voice] = []
        params: dict[str, Any] = {"page_size": 100}
        for _ in range(20):  # bounded: 2000 voices is far beyond any real account
            data = (await self._request("GET", "/v2/voices", params=params)).json()
            for item in data.get("voices", []):
                if not isinstance(item, dict) or not item.get("voice_id"):
                    continue
                voices.append(
                    Voice(
                        voice_id=str(item["voice_id"]),
                        name=item.get("name") or "Uten navn",
                        category=item.get("category") or "",
                        description=(item.get("description") or "")[:200],
                        preview_url=item.get("preview_url") or "",
                        labels={str(k): str(v) for k, v in (item.get("labels") or {}).items()},
                    )
                )
            token = data.get("next_page_token")
            if not data.get("has_more") or not token:
                break
            params = {"page_size": 100, "next_page_token": token}
        logger.info("Fetched %d ElevenLabs voices", len(voices))
        return voices

    async def list_models(self) -> list[TTSModel]:
        data = (await self._request("GET", "/v1/models")).json()
        models: list[TTSModel] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get("model_id"):
                continue
            if not item.get("can_do_text_to_speech", True):
                continue
            languages = [
                str(lang.get("name") or lang.get("language_id"))
                for lang in (item.get("languages") or [])
                if isinstance(lang, dict)
            ]
            models.append(
                TTSModel(
                    model_id=str(item["model_id"]),
                    name=item.get("name") or str(item["model_id"]),
                    description=(item.get("description") or "")[:300],
                    languages=languages,
                    max_characters=int(item.get("max_characters_request_subscribed_user") or 0),
                )
            )
        logger.info("Fetched %d ElevenLabs models", len(models))
        return models

    # -- synthesis -------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        model_id: str,
        voice_settings: dict[str, Any] | None = None,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
    ) -> SynthesisResult:
        """Convert plain text to MP3 bytes.

        The text is validated first: HTML must never reach a paid endpoint that
        bills per character.
        """
        if not voice_id:
            raise ConfigurationError("Ingen ElevenLabs-stemme er valgt.", provider=PROVIDER)
        if not model_id:
            raise ConfigurationError("Ingen ElevenLabs-modell er valgt.", provider=PROVIDER)

        clean = ensure_plain_text(text, context="teksten som skal leses opp")
        body: dict[str, Any] = {"text": clean, "model_id": model_id}
        if voice_settings:
            body["voice_settings"] = voice_settings

        logger.info(
            "Sending %s characters to ElevenLabs voice=%s model=%s",
            f"{len(clean):,}".replace(",", " "),
            voice_id,
            model_id,
        )

        async def _call() -> httpx.Response:
            response = await self._client.post(
                f"/v1/text-to-speech/{voice_id}",
                params={"output_format": output_format},
                json=body,
                headers={"Accept": "audio/mpeg"},
            )
            raise_for_status(response, provider=PROVIDER, messages=ERROR_MESSAGES)
            return response

        response = await with_retries(
            _call, provider=PROVIDER, description="ElevenLabs text-to-speech"
        )
        audio = response.content
        if not audio:
            raise UpstreamError("ElevenLabs returnerte en tom lydfil.", provider=PROVIDER)

        logger.info("Audio generation completed bytes=%d", len(audio))
        return SynthesisResult(
            audio=audio, characters=len(clean), voice_id=voice_id, model_id=model_id
        )


async def check_elevenlabs_connection(
    api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    async with ElevenLabsClient(api_key, transport=transport) as client:
        sub = await client.get_subscription()
    used = f"{sub.character_count:,}".replace(",", " ")
    limit = f"{sub.character_limit:,}".replace(",", " ")
    return {
        "ok": True,
        "detail": f"Tilkoblet. Abonnement «{sub.tier}», {used} av {limit} tegn brukt.",
        "tier": sub.tier,
        "character_count": sub.character_count,
        "character_limit": sub.character_limit,
    }
