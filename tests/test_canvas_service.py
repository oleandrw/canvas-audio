"""Canvas client: pagination, parsing and error mapping. No real network."""

from __future__ import annotations

import httpx
import pytest

from app.services.canvas_service import CanvasClient
from app.services.errors import (
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    TransientError,
)

BASE = "https://canvas.example.test"
TOKEN = "12345~token"


def build(handler) -> CanvasClient:
    return CanvasClient(BASE, TOKEN, transport=httpx.MockTransport(handler))


class TestAuthHeaders:
    async def test_sends_bearer_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"id": 1, "name": "Ola"})

        async with build(handler) as client:
            await client.test_connection()
        assert seen["authorization"] == f"Bearer {TOKEN}"

    def test_missing_url_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="Canvas-URL"):
            CanvasClient("", TOKEN)

    def test_missing_token_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="token"):
            CanvasClient(BASE, "")


class TestPagination:
    async def test_follows_link_header(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "page=2" in str(request.url):
                return httpx.Response(200, json=[{"id": 3, "name": "C"}])
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
                headers={"Link": f'<{BASE}/api/v1/courses?page=2>; rel="next"'},
            )

        async with build(handler) as client:
            courses = await client.list_courses()

        assert [c.id for c in courses] == ["1", "2", "3"]
        assert len(calls) == 2

    async def test_stops_without_a_next_link(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=[{"id": 1, "name": "A"}])

        async with build(handler) as client:
            await client.list_courses()
        assert len(calls) == 1


class TestParsing:
    async def test_modules_keep_order_and_only_pages_are_kept(self) -> None:
        payload = [
            {
                "id": 20,
                "name": "Modul 2",
                "position": 2,
                "items": [
                    {"title": "Quiz", "type": "Quiz", "position": 1},
                    {"title": "Side B", "type": "Page", "page_url": "side-b", "position": 2},
                ],
            },
            {
                "id": 10,
                "name": "Modul 1",
                "position": 1,
                "items": [
                    {"title": "Side A", "type": "Page", "page_url": "side-a", "position": 1},
                    {"title": "Fil", "type": "File", "position": 2},
                    {"title": "Lenke", "type": "ExternalUrl", "position": 3},
                ],
            },
        ]

        async with build(lambda _r: httpx.Response(200, json=payload)) as client:
            modules = await client.list_modules("4711")

        assert [m.name for m in modules] == ["Modul 1", "Modul 2"]
        pages = [item for m in modules for item in m.items if item.is_page]
        assert [p.page_url for p in pages] == ["side-a", "side-b"]
        assert all(not item.is_page for m in modules for item in m.items if item.type != "Page")

    async def test_page_body_and_timestamp_are_parsed(self) -> None:
        payload = {
            "page_id": 99,
            "url": "velkommen",
            "title": "Velkommen",
            "updated_at": "2026-03-01T10:15:00Z",
            "body": "<p>Hei</p>",
        }
        async with build(lambda _r: httpx.Response(200, json=payload)) as client:
            page = await client.get_page("4711", "velkommen")

        assert page.page_id == "99"
        assert page.title == "Velkommen"
        assert page.body == "<p>Hei</p>"
        assert page.updated_at is not None
        assert page.updated_at.year == 2026

    async def test_invalid_timestamp_is_tolerated(self) -> None:
        payload = {"page_id": 1, "url": "a", "title": "A", "updated_at": "not-a-date", "body": ""}
        async with build(lambda _r: httpx.Response(200, json=payload)) as client:
            page = await client.get_page("4711", "a")
        assert page.updated_at is None


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected", "fragment"),
        [
            (401, AuthenticationError, "401"),
            (403, AuthenticationError, "403"),
            (404, NotFoundError, "404"),
        ],
    )
    async def test_permanent_errors_are_not_retried(
        self, status: int, expected: type[Exception], fragment: str
    ) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(status, json={"errors": [{"message": "nope"}]})

        async with build(handler) as client:
            with pytest.raises(expected) as info:
                await client.list_courses()

        assert fragment in str(info.value)
        assert calls["n"] == 1, "permanent failures must not be retried"

    async def test_rate_limit_is_retried_then_surfaces(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})

        async with build(handler) as client:
            with pytest.raises(RateLimitError):
                await client.list_courses()
        assert calls["n"] > 1, "429 is transient and should be retried"

    async def test_server_error_is_transient(self) -> None:
        async with build(lambda _r: httpx.Response(503, text="boom")) as client:
            with pytest.raises(TransientError):
                await client.list_courses()

    async def test_timeout_becomes_a_transient_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        async with build(handler) as client:
            with pytest.raises(TransientError, match="Nettverksfeil"):
                await client.list_courses()

    async def test_connection_error_becomes_a_transient_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        async with build(handler) as client:
            with pytest.raises(TransientError):
                await client.list_courses()
