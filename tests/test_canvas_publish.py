"""Publishing audio into Canvas pages, against an in-memory Canvas."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import session_scope
from app.models.core import CanvasEmbed, Page
from app.services import canvas_publish as publish
from app.services.canvas_player import PlayerDesign, find_player_blocks, has_player
from app.services.errors import ConfigurationError
from app.services.hashing import audio_is_current
from tests.conftest import attach_audio, make_course, make_module, make_page

BASE = "https://canvas.example.test"
COURSE = "4711"


class FakeCanvas:
    """Pages, course files and the three-step upload, kept in memory."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.files: dict[str, dict[str, str]] = {}
        self.pending: dict[str, dict[str, str]] = {}
        self.requests: list[tuple[str, str]] = []
        self.next_id = 500
        self.strip_marker = False
        self.fail_put_for: frozenset[str] = frozenset()

    def page_json(self, url: str) -> dict[str, Any]:
        return {
            "url": url,
            "title": url,
            "body": self.pages[url],
            "updated_at": "2026-09-15T10:00:00Z",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, f"{request.url.host}{path}"))

        if request.url.host == "files.example.test":
            assert "authorization" not in request.headers
            meta = self.pending.pop(path.rsplit("/", 1)[-1])
            self.next_id += 1
            self.files[str(self.next_id)] = meta
            return httpx.Response(
                302, headers={"Location": f"{BASE}/api/v1/files/{self.next_id}/create_success"}
            )

        if method == "POST" and path == f"/api/v1/courses/{COURSE}/files":
            form = {k: v[0] for k, v in parse_qs(request.read().decode()).items()}
            token = f"t{self.next_id}-{len(self.pending)}"
            self.pending[token] = {"name": form["name"], "folder": form["parent_folder_path"]}
            return httpx.Response(
                200,
                json={
                    "upload_url": f"https://files.example.test/upload/{token}",
                    "upload_params": {"key": token},
                },
            )

        if match := re.fullmatch(r"/api/v1/files/(\d+)(/create_success)?", path):
            file_id = match.group(1)
            if file_id not in self.files:
                return httpx.Response(404, json={"errors": [{"message": "not found"}]})
            return httpx.Response(
                200, json={"id": int(file_id), "display_name": self.files[file_id]["name"]}
            )

        if match := re.fullmatch(rf"/api/v1/courses/{COURSE}/pages/([\w-]+)", path):
            url = match.group(1)
            if url not in self.pages:
                return httpx.Response(404, json={})
            if method == "PUT":
                if url in self.fail_put_for:
                    return httpx.Response(403, json={"message": "forbidden"})
                body = json.loads(request.read())["wiki_page"]["body"]
                if self.strip_marker:
                    body = body.replace('class="canvas-audio-player" data-canvas-audio="1" ', "")
                self.pages[url] = body
            return httpx.Response(200, json=self.page_json(url))

        return httpx.Response(404, json={"error": f"unexpected {method} {path}"})

    def count(self, method: str, fragment: str) -> int:
        return sum(1 for m, p in self.requests if m == method and fragment in p)


@pytest.fixture(autouse=True)
def _no_run_left_behind() -> Any:
    publish.reset_runs()
    yield
    publish.reset_runs()


@pytest.fixture()
def course(db: Session, env: Path) -> dict[str, Any]:
    course = make_course(db)
    module = make_module(db, course, "Modul 1 – Introduksjon", 1)
    pages = []
    for i in (1, 2, 3):
        page = make_page(db, course, module, f"Side {i}", position=i, text=f"Tekst {i}.")
        audio = attach_audio(db, page)
        target = env / "output" / audio.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ID3" + bytes([i]))
        pages.append(page)
    db.commit()
    return {
        "course": course,
        "module": module,
        "page_ids": [p.id for p in pages],
        "urls": [p.canvas_url for p in pages],
    }


@pytest.fixture()
def fake(course: dict[str, Any]) -> FakeCanvas:
    canvas = FakeCanvas()
    for i, url in enumerate(course["urls"], start=1):
        canvas.pages[url] = f"<p>Tekst {i}.</p>"
    return canvas


async def run(
    fake: FakeCanvas, action: str = "publish", module_ids: list[int] | None = None
) -> publish.PublishRun:
    with session_scope() as session:
        plan = publish.prepare(session, action, module_ids)
    current = publish.new_run(action, plan)
    return await publish.execute(current, plan, transport=httpx.MockTransport(fake.handler))


def embeds() -> dict[int, CanvasEmbed]:
    with session_scope() as session:
        rows = session.scalars(select(CanvasEmbed)).all()
        session.expunge_all()
    return {row.page_id: row for row in rows}


class TestPublishing:
    async def test_files_go_in_module_folders_and_players_on_top(
        self, course: dict[str, Any], fake: FakeCanvas, db: Session
    ) -> None:
        hashes = {p.id: p.original_hash for p in db.scalars(select(Page))}

        result = await run(fake)

        assert result.error == ""
        assert result.counts()["published"] == 3
        assert len(fake.files) == 3
        assert {f["folder"] for f in fake.files.values()} == {"Lydfiler/01 Modul 1 – Introduksjon"}
        for i, url in enumerate(course["urls"], start=1):
            body = fake.pages[url]
            ((start, end),) = find_player_blocks(body)
            assert start == 0
            assert f"/courses/{COURSE}/files/" in body[:end]
            assert body[end:] == f"\n<p>Tekst {i}.</p>", "the page itself is untouched"

        assert all(e.embedded and e.file_id for e in embeds().values())
        with session_scope() as session:
            for page in session.scalars(select(Page)):
                assert has_player(page.html)
                assert page.original_hash == hashes[page.id], "the player is never read aloud"
                assert audio_is_current(page)

    async def test_publishing_again_changes_nothing(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        await run(fake)
        bodies = dict(fake.pages)
        fake.requests.clear()

        result = await run(fake)

        assert result.counts()["unchanged"] == 3
        assert fake.pages == bodies
        assert not any(method in ("POST", "PUT") for method, _ in fake.requests)

    async def test_a_new_design_updates_pages_without_uploading_again(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        await run(fake)
        with session_scope() as session:
            publish.save_design(session, PlayerDesign(style="stripe"), "Lydfiler")
        uploads = fake.count("POST", "files.example.test")

        result = await run(fake)

        assert result.counts()["published"] == 3
        assert fake.count("POST", "files.example.test") == uploads
        for url in course["urls"]:
            assert "border-left: 4px solid" in fake.pages[url]
            assert len(find_player_blocks(fake.pages[url])) == 1

    async def test_new_audio_is_uploaded_again(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        await run(fake)
        with session_scope() as session:
            session.get(Page, course["page_ids"][0]).audio.file_hash = "new-audio"
        uploads = fake.count("POST", "files.example.test")

        result = await run(fake)

        assert [o.status for o in result.outcomes] == ["published", "unchanged", "unchanged"]
        assert fake.count("POST", "files.example.test") == uploads + 1

    async def test_a_file_deleted_in_canvas_is_uploaded_again(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        await run(fake)
        del fake.files[embeds()[course["page_ids"][0]].file_id]

        result = await run(fake)

        assert result.outcomes[0].status == "published"
        assert "lastet opp" in result.outcomes[0].message
        assert embeds()[course["page_ids"][0]].file_id in fake.files

    async def test_edits_made_in_canvas_since_the_sync_are_kept(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        fake.pages[course["urls"][0]] = "<p>Læreren har skrevet noe nytt.</p>"

        await run(fake)

        assert fake.pages[course["urls"][0]].endswith("\n<p>Læreren har skrevet noe nytt.</p>")

    async def test_one_failing_page_does_not_stop_the_rest(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        fake.fail_put_for = frozenset({course["urls"][1]})

        result = await run(fake)

        assert [o.status for o in result.outcomes] == ["published", "failed", "published"]
        assert embeds()[course["page_ids"][1]].last_error
        with session_scope() as session:
            page = session.get(Page, course["page_ids"][1])
            assert publish.page_state(page, publish.load_design(session).fingerprint) == "failed"

    async def test_removing_takes_the_player_off_and_keeps_the_files(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        await run(fake)
        files = dict(fake.files)

        result = await run(fake, "remove")

        assert result.counts()["removed"] == 3
        for i, url in enumerate(course["urls"], start=1):
            assert fake.pages[url] == f"<p>Tekst {i}.</p>"
        assert fake.files == files
        assert fake.count("DELETE", "") == 0
        assert all(not e.embedded and e.file_id for e in embeds().values())

        uploads = fake.count("POST", "files.example.test")
        again = await run(fake)
        assert again.counts()["published"] == 3
        assert fake.count("POST", "files.example.test") == uploads, "the files are reused"

    async def test_pages_without_current_audio_are_skipped(
        self, course: dict[str, Any], fake: FakeCanvas, db: Session
    ) -> None:
        make_page(db, course["course"], course["module"], "Side 4", position=4, text="Tekst 4.")
        db.commit()

        result = await run(fake)

        assert result.counts() == {
            "published": 3,
            "unchanged": 0,
            "removed": 0,
            "skipped": 1,
            "failed": 0,
        }
        assert not any("side-4" in path for _, path in fake.requests)

    async def test_canvas_dropping_the_marker_is_reported(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        fake.strip_marker = True

        result = await run(fake)

        assert {o.message for o in result.outcomes} == {publish.MARKER_LOST}

    async def test_the_folder_setting_is_used(
        self, course: dict[str, Any], fake: FakeCanvas
    ) -> None:
        with session_scope() as session:
            publish.save_design(session, PlayerDesign(), "Podkast/lyd")

        await run(fake)

        assert {f["folder"] for f in fake.files.values()} == {
            "Podkast-lyd/01 Modul 1 – Introduksjon"
        }

    def test_a_course_must_be_selected(self, db: Session) -> None:
        with pytest.raises(ConfigurationError, match="emne"):
            publish.prepare(db, "publish", None)

    async def test_only_one_run_at_a_time(self, course: dict[str, Any]) -> None:
        with session_scope() as session:
            plan = publish.prepare(session, "publish", None)
        publish.new_run("publish", plan)
        with pytest.raises(RuntimeError):
            publish.new_run("publish", plan)


class TestRoutes:
    def test_the_page_renders(self, client: TestClient, seeded: dict[str, Any]) -> None:
        response = client.get("/publiser-canvas")
        assert response.status_code == 200
        assert "Publiser i Canvas" in response.text
        assert "Modul 1 – Introduksjon" in response.text
        assert 'data-canvas-audio="1"' in response.text

    def test_saving_a_design(self, client: TestClient) -> None:
        response = client.put(
            "/api/canvas-player/design",
            json={
                "style": "stripe",
                "color": "#2E7D32",
                "label": "Hør",
                "icon": "🔊",
                "folder": "Lyd",
            },
        )
        assert response.status_code == 200
        with session_scope() as session:
            design = publish.load_design(session)
            assert (design.style, design.color, design.label, design.icon) == (
                "stripe",
                "#2e7d32",
                "Hør",
                "🔊",
            )
            assert publish.load_folder(session) == "Lyd"

    @pytest.mark.parametrize(
        ("field", "value"), [("color", "red"), ("style", "blink"), ("icon", "💣")]
    )
    def test_invalid_designs_are_rejected(self, client: TestClient, field: str, value: str) -> None:
        response = client.put("/api/canvas-player/design", json={field: value})
        assert response.status_code == 422

    def test_the_preview_plays_the_local_audio(self, client: TestClient) -> None:
        html = client.post("/api/canvas-player/preview", json={"page_id": 5}).json()["html"]
        assert 'data-canvas-audio="1"' in html
        assert 'src="/api/audio/5"' in html

    def test_a_course_is_needed_to_start(self, client: TestClient) -> None:
        response = client.post("/api/canvas-player/runs", json={"action": "publish"})
        assert response.status_code == 400

    def test_a_run_starts_once(
        self, client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launched: list[Any] = []
        monkeypatch.setattr(publish, "launch", lambda run, plan: launched.append(plan))

        response = client.post("/api/canvas-player/runs", json={"action": "publish"})

        assert response.status_code == 200
        body = response.json()
        assert body["running"] is True
        assert body["counts"]["skipped"] == 3
        assert len(launched) == 1
        assert client.post("/api/canvas-player/runs", json={"action": "publish"}).status_code == 409
        assert client.get("/api/canvas-player/runs/current").json()["run"]["id"] == body["id"]

    def test_nothing_to_remove(self, client: TestClient, seeded: dict[str, Any]) -> None:
        response = client.post("/api/canvas-player/runs", json={"action": "remove"})
        assert response.status_code == 400
        assert "avspiller" in response.json()["detail"]
