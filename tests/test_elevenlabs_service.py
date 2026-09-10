"""ElevenLabs client: discovery, synthesis and the HTML guard."""

from __future__ import annotations

import httpx
import pytest

from app.services.elevenlabs_service import ElevenLabsClient, check_elevenlabs_connection
from app.services.errors import (
    AuthenticationError,
    ConfigurationError,
    InvalidContentError,
    QuotaExceededError,
    UpstreamError,
)

KEY = "sk-dummy-elevenlabs-key"


def build(handler) -> ElevenLabsClient:
    return ElevenLabsClient(KEY, transport=httpx.MockTransport(handler))


class TestConfiguration:
    def test_missing_key_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="ElevenLabs"):
            ElevenLabsClient("")

    async def test_sends_the_api_key_header(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"tier": "creator", "character_count": 0})

        async with build(handler) as client:
            await client.get_subscription()
        assert seen["xi-api-key"] == KEY


class TestDiscovery:
    async def test_parses_voices_and_follows_pagination(self) -> None:
        pages = [
            {
                "voices": [
                    {
                        "voice_id": "v1",
                        "name": "Nora",
                        "category": "premade",
                        "labels": {"accent": "norwegian"},
                    }
                ],
                "has_more": True,
                "next_page_token": "tok",
            },
            {
                "voices": [{"voice_id": "v2", "name": "Even", "category": "cloned"}],
                "has_more": False,
            },
        ]
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            payload = pages[min(calls["n"], 1)]
            calls["n"] += 1
            return httpx.Response(200, json=payload)

        async with build(handler) as client:
            voices = await client.list_voices()

        assert [v.voice_id for v in voices] == ["v1", "v2"]
        assert voices[0].labels == {"accent": "norwegian"}

    async def test_models_without_tts_capability_are_filtered_out(self) -> None:
        payload = [
            {
                "model_id": "eleven_multilingual_v2",
                "name": "Multilingual v2",
                "can_do_text_to_speech": True,
                "languages": [{"language_id": "no", "name": "Norwegian"}],
                "max_characters_request_subscribed_user": 5000,
            },
            {"model_id": "scribe_v1", "name": "Scribe", "can_do_text_to_speech": False},
        ]
        async with build(lambda _r: httpx.Response(200, json=payload)) as client:
            models = await client.list_models()

        assert [m.model_id for m in models] == ["eleven_multilingual_v2"]
        assert models[0].languages == ["Norwegian"]
        assert models[0].max_characters == 5000

    async def test_subscription_reports_remaining_characters(self) -> None:
        payload = {"tier": "creator", "character_count": 40_000, "character_limit": 100_000}
        async with build(lambda _r: httpx.Response(200, json=payload)) as client:
            sub = await client.get_subscription()
        assert sub.remaining == 60_000

    async def test_connection_test_returns_a_readable_message(self) -> None:
        payload = {"tier": "creator", "character_count": 1000, "character_limit": 100_000}
        result = await check_elevenlabs_connection(
            KEY, transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        )
        assert result["ok"]
        assert "creator" in result["detail"]


class TestSynthesis:
    async def test_returns_audio_bytes_and_character_count(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.read().decode()
            return httpx.Response(200, content=b"ID3-fake-mp3-bytes")

        async with build(handler) as client:
            result = await client.synthesize(
                "Hei og velkommen.",
                voice_id="v1",
                model_id="eleven_multilingual_v2",
                voice_settings={"stability": 0.5},
            )

        assert result.audio == b"ID3-fake-mp3-bytes"
        assert result.characters == len("Hei og velkommen.")
        assert "/v1/text-to-speech/v1" in captured["url"]
        assert "output_format=mp3_44100_128" in captured["url"]
        assert "eleven_multilingual_v2" in captured["body"]

    async def test_html_never_reaches_the_paid_endpoint(self) -> None:
        called = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, content=b"x")

        async with build(handler) as client:
            with pytest.raises(InvalidContentError, match="HTML"):
                await client.synthesize(
                    "<p>ikke renset</p>", voice_id="v1", model_id="eleven_multilingual_v2"
                )
        assert called["n"] == 0, "no request may be made for unclean text"

    async def test_empty_text_is_rejected_before_the_call(self) -> None:
        async with build(lambda _r: httpx.Response(200, content=b"x")) as client:
            with pytest.raises(InvalidContentError):
                await client.synthesize("   ", voice_id="v1", model_id="m")

    async def test_missing_voice_selection_is_rejected(self) -> None:
        async with build(lambda _r: httpx.Response(200, content=b"x")) as client:
            with pytest.raises(ConfigurationError, match="stemme"):
                await client.synthesize("Hei", voice_id="", model_id="m")

    async def test_missing_model_selection_is_rejected(self) -> None:
        async with build(lambda _r: httpx.Response(200, content=b"x")) as client:
            with pytest.raises(ConfigurationError, match="modell"):
                await client.synthesize("Hei", voice_id="v1", model_id="")

    async def test_empty_audio_response_is_an_error(self) -> None:
        async with build(lambda _r: httpx.Response(200, content=b"")) as client:
            with pytest.raises(UpstreamError, match="tom lydfil"):
                await client.synthesize("Hei", voice_id="v1", model_id="m")


class TestErrors:
    async def test_invalid_key_is_an_authentication_error(self) -> None:
        async with build(lambda _r: httpx.Response(401, json={"detail": "bad key"})) as client:
            with pytest.raises(AuthenticationError, match="401"):
                await client.get_subscription()

    async def test_quota_exhausted_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(402, json={"detail": "quota"})

        async with build(handler) as client:
            with pytest.raises(QuotaExceededError, match="kvoten"):
                await client.synthesize("Hei", voice_id="v1", model_id="m")
        assert calls["n"] == 1
