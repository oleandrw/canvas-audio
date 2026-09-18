"""Synchronising a Canvas course into the database.

The invariant under test: a sync updates content and status, but never touches
generated audio and never triggers a paid call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.database import session_scope
from app.models.core import Course, GeneratedAudio, Module, Page, PageAudioStatus
from app.services.hashing import page_audio_status, text_hash
from app.services.sync_service import course_structure, sync_course
from app.services.text_service import html_to_text
from tests.conftest import attach_audio

COURSE = {"id": 4711, "name": "Kunstig intelligens", "course_code": "KI-101"}

MODULES = [
    {
        "id": 1,
        "name": "Modul 1 – Introduksjon",
        "position": 1,
        "items": [
            {"title": "Velkommen", "type": "Page", "page_url": "velkommen", "position": 1},
            {"title": "Quiz", "type": "Quiz", "position": 2},
            {"title": "Om emnet", "type": "Page", "page_url": "om-emnet", "position": 3},
        ],
    },
    {
        "id": 2,
        "name": "Modul 2 – Tema 1",
        "position": 2,
        "items": [
            {"title": "Fagstoff", "type": "Page", "page_url": "fagstoff", "position": 1},
            {"title": "Vedlegg", "type": "File", "position": 2},
        ],
    },
]

PAGES = [
    {"page_id": 10, "url": "velkommen", "title": "Velkommen", "updated_at": "2026-01-05T09:00:00Z"},
    {"page_id": 11, "url": "om-emnet", "title": "Om emnet", "updated_at": "2026-01-05T09:00:00Z"},
    {"page_id": 12, "url": "fagstoff", "title": "Fagstoff", "updated_at": "2026-01-05T09:00:00Z"},
    {"page_id": 13, "url": "løsblad", "title": "Løsblad", "updated_at": "2026-01-05T09:00:00Z"},
]

BODIES = {
    "velkommen": "<h2>Velkommen</h2><p>Hei og velkommen til emnet.</p>",
    "om-emnet": "<p>Emnet handler om <strong>KI</strong>.</p>",
    "fagstoff": "<p>Her kommer fagstoffet.</p><script>x()</script>",
    "løsblad": "<p>En side uten modul.</p>",
}


def canvas_transport(
    *, bodies: dict[str, str] | None = None, pages: list[dict[str, Any]] | None = None
) -> httpx.MockTransport:
    bodies = bodies if bodies is not None else BODIES
    pages = pages if pages is not None else PAGES

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/modules"):
            return httpx.Response(200, json=MODULES)
        if path.endswith("/pages"):
            return httpx.Response(200, json=pages)
        if "/pages/" in path:
            slug = path.rsplit("/", 1)[-1]
            from urllib.parse import unquote

            slug = unquote(slug)
            summary = next((p for p in pages if p["url"] == slug), None)
            if summary is None:
                return httpx.Response(404, json={"errors": [{"message": "not found"}]})
            payload = {**summary, "body": bodies.get(slug, "")}
            return httpx.Response(200, json=payload)
        if path.endswith("/courses/4711"):
            return httpx.Response(200, json=COURSE)
        return httpx.Response(404, text=json.dumps({"path": path}))

    return httpx.MockTransport(handler)


async def run_sync(**kwargs: Any):
    return await sync_course(
        base_url="https://canvas.example.test",
        token="12345~token",
        canvas_course_id="4711",
        transport=canvas_transport(**kwargs),
    )


class TestFirstSync:
    async def test_creates_course_modules_and_pages(self, env: Path) -> None:
        result = await run_sync()

        assert result.course_name == "Kunstig intelligens"
        assert result.modules == 2
        assert result.pages == 4
        assert result.new_pages == 4
        assert result.errors == []

        with session_scope() as session:
            course = session.scalar(select(Course))
            assert course.is_selected
            assert course.slug == "kunstig-intelligens"

            modules = session.execute(select(Module).order_by(Module.position)).scalars().all()
            assert [m.name for m in modules] == ["Modul 1 – Introduksjon", "Modul 2 – Tema 1"]
            assert modules[0].slug == "01-modul-1-introduksjon"

    async def test_pages_keep_their_module_and_canvas_order(self, env: Path) -> None:
        await run_sync()
        with session_scope() as session:
            pages = {p.title: p for p in session.execute(select(Page)).scalars()}
            assert pages["Velkommen"].position == 1
            assert pages["Om emnet"].position == 2  # the quiz between them is ignored
            assert pages["Velkommen"].module.name == "Modul 1 – Introduksjon"
            assert pages["Fagstoff"].module.name == "Modul 2 – Tema 1"

    async def test_pages_outside_a_module_are_kept_separately(self, env: Path) -> None:
        await run_sync()
        with session_scope() as session:
            loose = session.scalar(select(Page).where(Page.title == "Løsblad"))
            assert loose.module_id is None

            structure = course_structure(session, loose.course_id)
            assert structure["groups"][-1]["module"] is None
            assert [p.title for p in structure["groups"][-1]["pages"]] == ["Løsblad"]

    async def test_html_is_cleaned_on_the_way_in(self, env: Path) -> None:
        await run_sync()
        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Fagstoff"))
            assert page.original_text == "Her kommer fagstoffet."
            assert "script" not in page.original_text
            assert page.original_hash

    async def test_only_canvas_pages_are_imported(self, env: Path) -> None:
        await run_sync()
        with session_scope() as session:
            titles = {p.title for p in session.execute(select(Page)).scalars()}
            assert "Quiz" not in titles
            assert "Vedlegg" not in titles


class TestResync:
    async def test_unchanged_pages_are_not_refetched(self, env: Path) -> None:
        await run_sync()

        fetched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/modules"):
                return httpx.Response(200, json=MODULES)
            if path.endswith("/pages"):
                return httpx.Response(200, json=PAGES)
            if "/pages/" in path:
                from urllib.parse import unquote

                slug = unquote(path.rsplit("/", 1)[-1])
                fetched.append(slug)
                summary = next(p for p in PAGES if p["url"] == slug)
                return httpx.Response(200, json={**summary, "body": BODIES.get(slug, "")})
            return httpx.Response(200, json=COURSE)

        await sync_course(
            base_url="https://canvas.example.test",
            token="12345~token",
            canvas_course_id="4711",
            transport=httpx.MockTransport(handler),
        )
        assert fetched == [], "unchanged pages must not be fetched again"

    async def test_changed_page_is_detected_but_audio_is_left_alone(self, env: Path) -> None:
        await run_sync()

        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Velkommen"))
            attach_audio(session, page)
            page_id = page.id

        newer_pages = [
            {**p, "updated_at": "2026-06-01T10:00:00Z"} if p["url"] == "velkommen" else p
            for p in PAGES
        ]
        newer_bodies = {**BODIES, "velkommen": "<p>Helt ny tekst.</p>"}
        result = await run_sync(pages=newer_pages, bodies=newer_bodies)

        assert result.changed_pages == 1

        with session_scope() as session:
            page = session.get(Page, page_id)
            assert page.original_text == "Helt ny tekst."
            # The audio row is still there, but now flagged as out of date.
            assert page.audio is not None
            assert page_audio_status(page) == PageAudioStatus.CANVAS_CHANGED

    async def test_removed_pages_are_archived_not_deleted(self, env: Path) -> None:
        await run_sync()
        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Løsblad"))
            attach_audio(session, page)

        remaining = [p for p in PAGES if p["url"] != "løsblad"]
        result = await run_sync(pages=remaining)
        assert result.archived_pages == 1

        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Løsblad"))
            assert page is not None, "the row must survive so the audio history is not lost"
            assert page.archived
            assert session.scalar(select(GeneratedAudio).where(GeneratedAudio.page_id == page.id))

            structure = course_structure(session, page.course_id)
            listed = {p.title for group in structure["groups"] for p in group["pages"]}
            assert "Løsblad" not in listed
            assert [p.title for p in structure["archived"]] == ["Løsblad"]


class TestPartialFailure:
    async def test_one_unfetchable_page_does_not_fail_the_sync(self, env: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/modules"):
                return httpx.Response(200, json=MODULES)
            if path.endswith("/pages"):
                return httpx.Response(200, json=PAGES)
            if path.endswith("/om-emnet"):
                return httpx.Response(404, json={"errors": [{"message": "gone"}]})
            if "/pages/" in path:
                from urllib.parse import unquote

                slug = unquote(path.rsplit("/", 1)[-1])
                summary = next(p for p in PAGES if p["url"] == slug)
                return httpx.Response(200, json={**summary, "body": BODIES.get(slug, "")})
            return httpx.Response(200, json=COURSE)

        result = await sync_course(
            base_url="https://canvas.example.test",
            token="12345~token",
            canvas_course_id="4711",
            transport=httpx.MockTransport(handler),
        )

        assert len(result.errors) == 1
        assert "om-emnet" in result.errors[0]

        with session_scope() as session:
            failed = session.scalar(select(Page).where(Page.title == "Om emnet"))
            assert failed.last_error
            others = session.execute(select(Page).where(Page.title == "Fagstoff")).scalar_one()
            assert others.original_text, "the rest of the sync must still complete"


class TestSyncEndpoint:
    def test_sync_route_returns_a_summary(self, client, monkeypatch) -> None:
        from app.routes import canvas as canvas_routes

        real_client = canvas_routes.CanvasClient

        def patched(*args: Any, **kwargs: Any) -> Any:
            kwargs["transport"] = canvas_transport()
            return real_client(*args, **kwargs)

        monkeypatch.setattr("app.services.sync_service.CanvasClient", patched)

        response = client.post("/api/canvas/sync", json={"course_id": "4711"})
        assert response.status_code == 200
        body = response.json()
        assert body["modules"] == 2
        assert body["pages"] == 4
        assert "Synkronisert" in body["message"]

    def test_empty_course_id_is_rejected(self, client) -> None:
        assert client.post("/api/canvas/sync", json={"course_id": "  "}).status_code == 422


class TestRecleanOnSync:
    async def test_resync_applies_the_current_cleaner_to_stored_pages(self, env: Path) -> None:
        await run_sync()
        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Velkommen"))
            # What an older version of the cleaner could have left behind.
            page.original_text = "Velkommen\n\nHei og velkommen til emnet"
            page.original_hash = text_hash(page.original_text)

        result = await run_sync()
        assert result.changed_pages == 1

        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Velkommen"))
            assert page.original_text == html_to_text(page.html)
            assert page.original_hash == text_hash(page.original_text)


class TestProgress:
    """A long synchronisation says how far it has come."""

    async def test_it_counts_the_pages_as_they_arrive(self, env: Path) -> None:
        from app.services import sync_progress

        seen: list[dict[str, Any]] = []

        def record(**kwargs: Any) -> None:
            seen.append(dict(kwargs))

        await sync_course(
            base_url="https://canvas.example.test",
            token="12345~token",
            canvas_course_id="4711",
            transport=canvas_transport(),
            progress=record,
        )

        phases = [call["phase"] for call in seen if "phase" in call]
        assert "Henter sideinnhold" in phases
        assert "Rydder teksten" in phases
        done = [call["done"] for call in seen if "done" in call]
        assert done[0] == 0
        assert max(done) == 4, "every page is counted"
        assert done == sorted(done), "the count never goes backwards"
        sync_progress.reset()

    async def test_the_status_endpoint_reports_the_run(self, client: Any) -> None:
        from app.services import sync_progress

        sync_progress.reset()
        assert client.get("/api/canvas/sync/status").json()["running"] is False

        sync_progress.start("Mitt emne")
        sync_progress.update(phase="Henter sideinnhold", done=30, total=120)
        body = client.get("/api/canvas/sync/status").json()

        assert body == {
            "running": True,
            "course_name": "Mitt emne",
            "phase": "Henter sideinnhold",
            "done": 30,
            "total": 120,
            "message": "",
            "started_at": body["started_at"],
            "percent": 25,
        }

        sync_progress.finish("ferdig")
        assert client.get("/api/canvas/sync/status").json()["running"] is False
        sync_progress.reset()

    async def test_progress_from_a_finished_run_is_ignored(self) -> None:
        from app.services import sync_progress

        sync_progress.start("Mitt emne")
        sync_progress.finish("ferdig")
        sync_progress.update(done=99, total=99)

        assert sync_progress.snapshot()["done"] == 0
        sync_progress.reset()
