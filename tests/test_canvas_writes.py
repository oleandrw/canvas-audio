"""Canvas writes: the three-step file upload and saving a page body. No real network."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.services.canvas_service import CanvasClient
from app.services.errors import UpstreamError

BASE = "https://canvas.example.test"
TOKEN = "12345~token"
UPLOAD_URL = "https://files.example.test/upload/abc"


def build(handler) -> CanvasClient:
    return CanvasClient(BASE, TOKEN, transport=httpx.MockTransport(handler))


@pytest.fixture()
def mp3(tmp_path: Path) -> Path:
    path = tmp_path / "01-velkommen.mp3"
    path.write_bytes(b"ID3-fake-audio")
    return path


def ticket() -> httpx.Response:
    return httpx.Response(
        200, json={"upload_url": UPLOAD_URL, "upload_params": {"key": "abc", "acl": "private"}}
    )


async def upload(client: CanvasClient, mp3: Path):
    return await client.upload_file(
        "4711", path=mp3, name="01-velkommen.mp3", folder_path="Lydfiler/01 Modul 1"
    )


class TestUpload:
    async def test_the_documented_three_steps(self, mp3: Path) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/api/v1/courses/4711/files":
                return ticket()
            if request.url.host == "files.example.test":
                return httpx.Response(
                    301, headers={"Location": f"{BASE}/api/v1/files/99/create_success?uuid=u"}
                )
            return httpx.Response(
                200, json={"id": 99, "display_name": "01-velkommen.mp3", "folder_id": 7}
            )

        async with build(handler) as client:
            uploaded = await upload(client, mp3)

        assert uploaded.id == "99"
        assert uploaded.folder_id == "7"
        first, second, third = seen

        form = {k: v[0] for k, v in parse_qs(first.read().decode()).items()}
        assert form == {
            "name": "01-velkommen.mp3",
            "size": "14",
            "content_type": "audio/mpeg",
            "parent_folder_path": "Lydfiler/01 Modul 1",
            "on_duplicate": "overwrite",
        }
        assert first.headers["authorization"] == f"Bearer {TOKEN}"

        assert "authorization" not in second.headers, "the token must not go to the upload host"
        body = second.read()
        assert body.index(b'name="key"') < body.index(b'name="file"'), "the file goes last"
        assert b"ID3-fake-audio" in body

        assert third.method == "GET"
        assert third.url.path == "/api/v1/files/99/create_success"
        assert third.headers["authorization"] == f"Bearer {TOKEN}"

    async def test_a_201_with_the_file_needs_no_confirmation(self, mp3: Path) -> None:
        hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            if request.url.host == "files.example.test":
                return httpx.Response(201, json={"id": 5, "display_name": "a.mp3"})
            return ticket()

        async with build(handler) as client:
            uploaded = await upload(client, mp3)

        assert uploaded.id == "5"
        assert hosts == ["canvas.example.test", "files.example.test"]

    async def test_a_201_with_a_location_is_followed(self, mp3: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "files.example.test":
                return httpx.Response(201, json={"location": f"{BASE}/api/v1/files/6"})
            if request.url.path == "/api/v1/files/6":
                return httpx.Response(200, json={"id": 6})
            return ticket()

        async with build(handler) as client:
            assert (await upload(client, mp3)).id == "6"

    async def test_the_token_is_never_sent_to_another_host(self, mp3: Path) -> None:
        confirm_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/courses/4711/files":
                return ticket()
            if request.url.path.startswith("/upload"):
                return httpx.Response(
                    303, headers={"Location": "https://files.example.test/confirm/7"}
                )
            confirm_headers.update(request.headers)
            return httpx.Response(200, json={"id": 7})

        async with build(handler) as client:
            assert (await upload(client, mp3)).id == "7"
        assert "authorization" not in confirm_headers

    async def test_a_missing_upload_url_is_an_error(self, mp3: Path) -> None:
        async with build(lambda _r: httpx.Response(200, json={})) as client:
            with pytest.raises(UpstreamError, match="opplastingsadresse"):
                await upload(client, mp3)


class TestFilesAndPages:
    async def test_a_deleted_file_is_none(self) -> None:
        async with build(lambda _r: httpx.Response(404, json={})) as client:
            assert await client.get_file("1") is None

    async def test_an_existing_file(self) -> None:
        payload = {"id": 3, "display_name": "x.mp3"}
        async with build(lambda _r: httpx.Response(200, json=payload)) as client:
            found = await client.get_file("3")
        assert found is not None
        assert found.id == "3"

    async def test_saving_a_page_body_sends_no_notification(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["json"] = json.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "url": "side-1",
                    "title": "Side 1",
                    "body": "<p>ny</p>",
                    "updated_at": "2026-09-15T10:00:00Z",
                },
            )

        async with build(handler) as client:
            page = await client.update_page_body("4711", "side-1", "<p>ny</p>")

        assert captured == {
            "method": "PUT",
            "path": "/api/v1/courses/4711/pages/side-1",
            "json": {"wiki_page": {"body": "<p>ny</p>", "notify_of_update": False}},
        }
        assert page.body == "<p>ny</p>"
