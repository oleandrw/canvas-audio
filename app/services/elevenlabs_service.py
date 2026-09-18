"""ElevenLabs text-to-speech client.

Voices and models are always discovered from the API — nothing is hardcoded, and
the GUI refuses to start a job until the user has picked both.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.logging_config import get_logger
from app.services.audio_service import join_mp3
from app.services.errors import (
    AuthenticationError,
    ConfigurationError,
    ServiceError,
    UpstreamError,
)
from app.services.http import build_client, raise_for_status, with_retries
from app.services.text_service import ensure_plain_text, split_for_tts

logger = get_logger("app.services.elevenlabs")

PROVIDER = "elevenlabs"
BASE_URL = "https://api.elevenlabs.io"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

#: 400, 413 and 422 are deliberately absent: those are the answers a rejected text
#: gets, and ElevenLabs' own explanation is more useful than anything canned. The
#: length of the part that was rejected is added to it in :func:`_length_hint`.
ERROR_MESSAGES = {
    401: "ElevenLabs avviste API-nøkkelen (401).",
    402: "ElevenLabs-kvoten er brukt opp (402). Fyll på abonnementet for å fortsette.",
    429: "ElevenLabs begrenser antall forespørsler (429).",
}

#: How many characters to send per request when the model's own limit is unknown.
#: Low on purpose: every current model accepts at least this much.
FALLBACK_MAX_CHARACTERS = 4_000
#: Leave room under the model's stated limit; it counts characters its own way.
BUDGET_MARGIN = 0.95
MIN_BUDGET = 500
#: Neighbouring text sent along so the voice does not change tone at a seam.
STITCH_CONTEXT = 500
#: Statuses that mean "this text was not accepted", where the length matters.
REJECTED_TEXT_CODES = (400, 413, 422)


INVALID_KEY_MESSAGE = (
    "ElevenLabs sier at API-nøkkelen er ugyldig ({code}). Sjekk at hele nøkkelen er "
    "kopiert uten mellomrom, og at den ikke er slettet eller deaktivert på "
    "elevenlabs.io/app/settings/api-keys."
)
LIMITED_KEY_MESSAGE = (
    "ElevenLabs avviste nøkkelen for denne handlingen ({code}): «{message}». Nøkkelen er "
    "trolig begrenset. Rediger den på elevenlabs.io/app/settings/api-keys og gi den "
    "tilgang til tekst til tale, stemmer og modeller, og gjerne brukerinformasjon så "
    "appen kan vise forbruket."
)
REJECTED_KEY_MESSAGE = "ElevenLabs avviste API-nøkkelen ({code}): «{message}»."


class InvalidKeyError(AuthenticationError):
    """The key itself is wrong, as opposed to valid but not allowed to do this."""


def _auth_problem(response: httpx.Response) -> tuple[str, str] | None:
    """ElevenLabs' own ``(status, message)`` for a 401/403, if the body has one."""
    if response.status_code not in (401, 403):
        return None
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(detail, dict):
        return str(detail.get("status") or ""), str(detail.get("message") or "")
    if isinstance(detail, str) and detail:
        return "", detail
    return None


def _raise_for_auth(response: httpx.Response) -> None:
    """Turn ElevenLabs' auth errors into messages that say what is actually wrong."""
    problem = _auth_problem(response)
    if problem is None:
        return
    status, message = problem
    code = response.status_code
    if status == "invalid_api_key":
        text = INVALID_KEY_MESSAGE.format(code=code)
        raise InvalidKeyError(text, provider=PROVIDER, status_code=code)
    restricted = code == 403 or "permission" in status or "scope" in status
    template = LIMITED_KEY_MESSAGE if restricted else REJECTED_KEY_MESSAGE
    text = template.format(code=code, message=message or status or "ingen forklaring")
    raise AuthenticationError(text, provider=PROVIDER, status_code=code)


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
    #: How many requests the text needed. More than one means it was split.
    parts: int = 1


def _length_hint(exc: UpstreamError, *, chunk: str, part: int, parts: int) -> UpstreamError:
    """Add what the app knows and the API does not say: how long the text was."""
    if exc.status_code not in REJECTED_TEXT_CODES:
        return exc
    where = f"Del {part} av {parts}" if parts > 1 else "Teksten"
    size = f"{len(chunk):,}".replace(",", " ")
    return UpstreamError(
        f"{exc.message} {where} var {size} tegn.",
        provider=PROVIDER,
        status_code=exc.status_code,
    )


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
        #: model_id -> characters per request, read once and remembered.
        self._limits: dict[str, int] | None = None

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
            _raise_for_auth(response)
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

    async def budget_for(self, model_id: str) -> int:
        """Characters per request for this model, straight from the API.

        The limit differs a lot between models — 40 000 for flash and turbo, 10 000
        for multilingual v2, a few thousand for v3 — so it is read rather than
        guessed. If the list cannot be fetched, the conservative fallback is used.
        """
        if self._limits is None:
            try:
                self._limits = {m.model_id: m.max_characters for m in await self.list_models()}
            except ServiceError as exc:
                logger.warning("Could not read ElevenLabs model limits: %s", exc)
                self._limits = {}
        limit = self._limits.get(model_id) or FALLBACK_MAX_CHARACTERS
        return max(MIN_BUDGET, int(limit * BUDGET_MARGIN))

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        model_id: str,
        voice_settings: dict[str, Any] | None = None,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> SynthesisResult:
        """Convert plain text to MP3 bytes, in as many takes as the model needs.

        The text is validated first: HTML must never reach a paid endpoint that
        bills per character. A page longer than the model's per-request limit is
        split on sentence boundaries and the parts are joined back into one file.
        That costs the same, since the price follows the characters, not the number
        of requests.
        """
        if not voice_id:
            raise ConfigurationError("Ingen ElevenLabs-stemme er valgt.", provider=PROVIDER)
        if not model_id:
            raise ConfigurationError("Ingen ElevenLabs-modell er valgt.", provider=PROVIDER)

        clean = ensure_plain_text(text, context="teksten som skal leses opp")
        # Short pages are the normal case, and they never need the model list.
        budget = FALLBACK_MAX_CHARACTERS
        if len(clean) > FALLBACK_MAX_CHARACTERS:
            budget = await self.budget_for(model_id)
        chunks = split_for_tts(clean, budget)

        logger.info(
            "Sending %s characters to ElevenLabs in %d part(s) voice=%s model=%s",
            f"{len(clean):,}".replace(",", " "),
            len(chunks),
            voice_id,
            model_id,
        )

        pieces: list[bytes] = []
        for index, chunk in enumerate(chunks):
            pieces.append(
                await self._synthesize_part(
                    chunk,
                    voice_id=voice_id,
                    model_id=model_id,
                    voice_settings=voice_settings,
                    output_format=output_format,
                    previous_text=chunks[index - 1] if index else "",
                    next_text=chunks[index + 1] if index + 1 < len(chunks) else "",
                    part=index + 1,
                    parts=len(chunks),
                )
            )
            if progress is not None:
                await progress(index + 1, len(chunks))

        # Same voice, same format: the MP3 frames play as one file when joined.
        audio = join_mp3(pieces)
        logger.info("Audio generation completed parts=%d bytes=%d", len(chunks), len(audio))
        return SynthesisResult(
            audio=audio,
            characters=len(clean),
            voice_id=voice_id,
            model_id=model_id,
            parts=len(chunks),
        )

    async def _synthesize_part(
        self,
        chunk: str,
        *,
        voice_id: str,
        model_id: str,
        voice_settings: dict[str, Any] | None,
        output_format: str,
        previous_text: str,
        next_text: str,
        part: int,
        parts: int,
    ) -> bytes:
        body: dict[str, Any] = {"text": chunk, "model_id": model_id}
        if voice_settings:
            body["voice_settings"] = voice_settings
        # Request stitching: the model reads the neighbouring text for context, so
        # the tone does not jump where one part ends and the next begins.
        if previous_text:
            body["previous_text"] = previous_text[-STITCH_CONTEXT:]
        if next_text:
            body["next_text"] = next_text[:STITCH_CONTEXT]

        async def _call() -> httpx.Response:
            response = await self._client.post(
                f"/v1/text-to-speech/{voice_id}",
                params={"output_format": output_format},
                json=body,
                headers={"Accept": "audio/mpeg"},
            )
            _raise_for_auth(response)
            raise_for_status(response, provider=PROVIDER, messages=ERROR_MESSAGES)
            return response

        try:
            response = await with_retries(
                _call,
                provider=PROVIDER,
                description=f"ElevenLabs tekst til tale (del {part} av {parts})",
            )
        except UpstreamError as exc:
            raise _length_hint(exc, chunk=chunk, part=part, parts=parts) from exc

        audio = response.content
        if not audio:
            raise UpstreamError(
                f"ElevenLabs returnerte en tom lydfil (del {part} av {parts}).", provider=PROVIDER
            )
        return audio


async def check_elevenlabs_connection(
    api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Used by the Settings page's "Test ElevenLabs" button.

    A key that is valid but not allowed to read account details still passes,
    as long as it can do the work the app needs; the reply says what is missing.
    """
    async with ElevenLabsClient(api_key, transport=transport) as client:
        try:
            sub = await client.get_subscription()
        except InvalidKeyError:
            raise
        except AuthenticationError:
            await client.list_models()  # raises if the key cannot do this either
            return {
                "ok": True,
                "detail": (
                    "Nøkkelen virker, men har ikke tilgang til brukerinformasjon, så forbruk "
                    "og kvote kan ikke vises. Gi den den tilgangen på "
                    "elevenlabs.io/app/settings/api-keys hvis du vil se det."
                ),
            }
    used = f"{sub.character_count:,}".replace(",", " ")
    limit = f"{sub.character_limit:,}".replace(",", " ")
    return {
        "ok": True,
        "detail": f"Tilkoblet. Abonnement «{sub.tier}», {used} av {limit} tegn brukt.",
        "tier": sub.tier,
        "character_count": sub.character_count,
        "character_limit": sub.character_limit,
    }


#: A voice sample is at most a few hundred kilobytes; anything larger is not one.
MAX_PREVIEW_BYTES = 5_000_000


async def fetch_preview(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    """Download a voice sample from the URL ElevenLabs gave for that voice.

    The sample lives on ElevenLabs' file storage, not on the API host, so this
    deliberately uses a client without the API key: the key is for the API and
    must not be sent anywhere else.
    """
    if not url.startswith("https://"):
        raise UpstreamError("Lydprøven har en ugyldig adresse.", provider=PROVIDER)
    async with build_client(timeout=30.0, transport=transport) as client:
        response = await client.get(url)
        if response.status_code != 200:
            raise UpstreamError(
                f"Kunne ikke hente lydprøven ({response.status_code}).",
                provider=PROVIDER,
                status_code=response.status_code,
            )
        audio = response.content
    if len(audio) > MAX_PREVIEW_BYTES:
        raise UpstreamError("Lydprøven var uventet stor.", provider=PROVIDER)
    return audio
