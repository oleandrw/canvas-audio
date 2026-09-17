"""ElevenLabs client: discovery, synthesis and the HTML guard."""

from __future__ import annotations

import httpx
import pytest

from app.services.elevenlabs_service import (
    ElevenLabsClient,
    InvalidKeyError,
    check_elevenlabs_connection,
)
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


def auth_error(status: str, message: str, code: int = 401) -> httpx.Response:
    """The shape ElevenLabs uses; the invalid-key variant was observed directly."""
    detail = {"type": "authentication_error", "status": status, "message": message}
    return httpx.Response(code, json={"detail": detail})


MISSING_USER_READ = "The API key you used is missing the permission user_read."


class TestKeyProblems:
    async def test_a_wrong_key_says_so_and_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return auth_error("invalid_api_key", "Invalid API key")

        async with build(handler) as client:
            with pytest.raises(InvalidKeyError, match="ugyldig"):
                await client.get_subscription()
        assert calls["n"] == 1

    async def test_a_restricted_key_shows_elevenlabs_own_explanation(self) -> None:
        handler = lambda _r: auth_error("missing_permissions", MISSING_USER_READ)  # noqa: E731
        async with build(handler) as client:
            with pytest.raises(AuthenticationError) as info:
                await client.get_subscription()
        assert not isinstance(info.value, InvalidKeyError)
        assert "user_read" in info.value.message
        assert "begrenset" in info.value.message

    async def test_a_403_is_treated_as_a_restricted_key(self) -> None:
        handler = lambda _r: auth_error("forbidden", "Not allowed", code=403)  # noqa: E731
        async with build(handler) as client:
            with pytest.raises(AuthenticationError, match="begrenset"):
                await client.list_voices()

    async def test_an_unknown_rejection_still_shows_the_reason(self) -> None:
        handler = lambda _r: httpx.Response(401, json={"detail": "bad key"})  # noqa: E731
        async with build(handler) as client:
            with pytest.raises(AuthenticationError, match="bad key"):
                await client.get_subscription()

    async def test_synthesis_reports_the_same_way(self) -> None:
        handler = lambda _r: auth_error("invalid_api_key", "Invalid API key")  # noqa: E731
        async with build(handler) as client:
            with pytest.raises(InvalidKeyError):
                await client.synthesize("Hei", voice_id="v1", model_id="m")


class TestConnectionCheck:
    async def test_a_valid_key_without_account_access_still_passes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/user/subscription":
                return auth_error("missing_permissions", MISSING_USER_READ)
            model = {
                "model_id": "eleven_multilingual_v2",
                "name": "M",
                "can_do_text_to_speech": True,
            }
            return httpx.Response(200, json=[model])

        result = await check_elevenlabs_connection(KEY, transport=httpx.MockTransport(handler))
        assert result["ok"] is True
        assert "brukerinformasjon" in result["detail"]

    async def test_fails_when_the_key_cannot_do_the_work_either(self) -> None:
        missing = "missing the permission models_read"
        handler = lambda _r: auth_error("missing_permissions", missing)  # noqa: E731
        with pytest.raises(AuthenticationError, match="models_read"):
            await check_elevenlabs_connection(KEY, transport=httpx.MockTransport(handler))

    async def test_stops_at_a_wrong_key_without_trying_further(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return auth_error("invalid_api_key", "Invalid API key")

        with pytest.raises(InvalidKeyError):
            await check_elevenlabs_connection(KEY, transport=httpx.MockTransport(handler))
        assert paths == ["/v1/user/subscription"]
