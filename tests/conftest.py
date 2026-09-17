"""Shared test fixtures.

Two guarantees this file enforces for the whole suite:

1. **No real network.** The real httpx transports are disabled, so a forgotten
   mock fails loudly instead of calling a paid API.
2. **No real money.** The background worker's ``run_job`` is replaced with a
   recorder, and every credential is a dummy value.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import config
from app.config import reset_store
from app.database import get_session_factory, init_db, reset_engine, session_scope
from app.jobs import queue as queue_module
from app.models.core import Course, GeneratedAudio, Module, Page
from app.pricing import reset_pricing_cache
from app.services import http as http_module
from app.services import update_service
from app.services.hashing import text_hash
from app.services.naming import course_dirname, module_dirname

DUMMY_ENV = {
    "CANVAS_URL": "https://canvas.example.test",
    "CANVAS_TOKEN": "12345~dummy-canvas-token-for-tests",
    "ELEVENLABS_API_KEY": "sk-dummy-elevenlabs-key-for-tests",
    "OPENAI_API_KEY": "sk-dummy-openai-key-for-tests",
    "ANTHROPIC_API_KEY": "sk-ant-dummy-anthropic-key-for-tests",
    "SOUNDCLOUD_CLIENT_ID": "dummy-client-id",
    "SOUNDCLOUD_CLIENT_SECRET": "dummy-client-secret",
    "SOUNDCLOUD_REDIRECT_URI": "http://testserver/api/soundcloud/callback",
    "SECRET_KEY": "test-secret-key-not-used-anywhere-real",
    "APP_PASSWORD": "",
    "LOG_LEVEL": "WARNING",
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if any test tries to reach the internet."""

    async def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "Network access is disabled in tests. Use httpx.MockTransport or a fake client."
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked, raising=False)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=False)
    try:  # the anthropic SDK ships its own httpx fork
        import httpx2

        monkeypatch.setattr(
            httpx2.AsyncHTTPTransport, "handle_async_request", _blocked, raising=False
        )
    except ImportError:  # pragma: no cover - optional dependency
        pass


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same promise as _no_network: the suite never runs git against a real checkout.

    The block sits on ``subprocess.run`` rather than on ``_run_git``, so a test may
    still fake either level.
    """

    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Git is disabled in tests. Fake update_service._run_git.")

    monkeypatch.setattr(subprocess, "run", _blocked)
    update_service.reset_state()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry/backoff logic under test without the real waiting."""
    monkeypatch.setattr(http_module, "DEFAULT_BASE_DELAY", 0.001)
    monkeypatch.setattr(http_module, "MAX_DELAY", 0.01)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated data/output directories and dummy credentials."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    data_dir.mkdir()
    output_dir.mkdir()

    # A stray reset_store() must never reach the developer's own .env file.
    monkeypatch.setattr(config, "DEFAULT_ENV_PATH", tmp_path / ".env")

    for key, value in DUMMY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")

    reset_store(env_path=tmp_path / ".env")
    reset_pricing_cache()
    reset_engine()
    queue_module.reset_queue()
    init_db()
    yield tmp_path
    reset_engine()


@pytest.fixture()
def db(env: Path) -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def recorded_jobs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the job runner so queued jobs are recorded, never executed."""
    seen: list[int] = []

    async def _fake_run_job(job_id: int, **_kwargs: Any) -> None:
        seen.append(job_id)

    monkeypatch.setattr(queue_module, "run_job", _fake_run_job)
    return seen


@pytest.fixture()
def client(env: Path, recorded_jobs: list[int]) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Data builders
# --------------------------------------------------------------------------- #


def make_course(session: Session, name: str = "Mitt emne") -> Course:
    course = Course(
        canvas_course_id="4711",
        name=name,
        course_code="EMNE-101",
        slug=course_dirname(name),
        is_selected=True,
        synced_at=datetime.now(UTC),
    )
    session.add(course)
    session.flush()
    return course


def make_module(session: Session, course: Course, name: str, position: int) -> Module:
    module = Module(
        course_id=course.id,
        canvas_module_id=f"m{position}",
        name=name,
        position=position,
        slug=module_dirname(name, position),
    )
    session.add(module)
    session.flush()
    return module


def make_page(
    session: Session,
    course: Course,
    module: Module | None,
    title: str,
    *,
    position: int = 1,
    text: str = "Dette er innholdet på siden.",
    processed_text: str = "",
) -> Page:
    page = Page(
        course_id=course.id,
        module_id=module.id if module else None,
        canvas_page_id=f"p-{title.lower().replace(' ', '-')}",
        canvas_url=title.lower().replace(" ", "-"),
        title=title,
        position=position,
        html=f"<p>{text}</p>",
        original_text=text,
        original_hash=text_hash(text),
        processed_text=processed_text,
        processed_hash=text_hash(processed_text) if processed_text else "",
    )
    session.add(page)
    session.flush()
    return page


def attach_audio(session: Session, page: Page, *, stale: bool = False) -> GeneratedAudio:
    audio = GeneratedAudio(
        page_id=page.id,
        relative_path=f"mitt-emne/01-modul/{page.position:02d}-{page.canvas_page_id}.mp3",
        original_title=page.title,
        size_bytes=1024,
        duration_seconds=12.5,
        voice_id="voice-1",
        voice_name="Testrøst",
        model_id="eleven_multilingual_v2",
        character_count=len(page.tts_text),
        source_text_hash="stale-hash" if stale else text_hash(page.tts_text),
        source_canvas_hash=page.original_hash,
        file_hash="filehash",
    )
    session.add(audio)
    session.flush()
    return audio


@pytest.fixture()
def seeded(db: Session) -> dict[str, Any]:
    """A small course: two modules, four pages, one with up-to-date audio."""
    course = make_course(db)
    m1 = make_module(db, course, "Modul 1 – Introduksjon", 1)
    m2 = make_module(db, course, "Modul 2 – Tema 1", 2)

    p1 = make_page(db, course, m1, "Velkommen", position=1, text="Velkommen til emnet.")
    p2 = make_page(db, course, m1, "Hva skal vi lære?", position=2, text="Vi skal lære mye.")
    p3 = make_page(db, course, m2, "Introduksjon", position=1, text="En introduksjon til temaet.")
    p4 = make_page(db, course, m2, "Oppsummering", position=2, text="Kort oppsummering.")

    attach_audio(db, p1)
    db.commit()
    for obj in (course, m1, m2, p1, p2, p3, p4):
        db.refresh(obj)
    return {"course": course, "modules": [m1, m2], "pages": [p1, p2, p3, p4]}


def run_async(coro: Any) -> Any:
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)


def mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def commit(session: Session) -> None:
    session.commit()


__all__ = [
    "attach_audio",
    "commit",
    "make_course",
    "make_module",
    "make_page",
    "mock_transport",
    "run_async",
    "session_scope",
]
