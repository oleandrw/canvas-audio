"""End-to-end generation pipeline with injected fakes.

This is the closest the automated suite gets to the real workflow: Canvas HTML
→ cleaned text → optional AI → "text-to-speech" → an MP3 on disk → the playback
route. No paid API is involved; the ElevenLabs and AI clients are fakes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import session_scope
from app.jobs.pipeline import run_job
from app.models.core import (
    AIProvider,
    GeneratedAudio,
    GenerationJob,
    ItemStatus,
    JobItem,
    JobStatus,
    Page,
    UsageRecord,
)
from app.services.ai_common import AIResult
from app.services.elevenlabs_service import SynthesisResult
from app.services.text_service import html_to_text
from tests.conftest import make_course, make_module, make_page

MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00fake-mp3-payload"


class FakeElevenLabs:
    """Records what it was asked to synthesise and returns fixed bytes."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on = fail_on or set()

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        model_id: str,
        voice_settings: dict[str, Any] | None = None,
    ) -> SynthesisResult:
        from app.services.errors import UpstreamError
        from app.services.text_service import ensure_plain_text

        ensure_plain_text(text)  # the real client does this too
        self.calls.append(
            {"text": text, "voice_id": voice_id, "model_id": model_id, "settings": voice_settings}
        )
        if text in self.fail_on:
            raise UpstreamError("ElevenLabs svarte 500", provider="elevenlabs")
        return SynthesisResult(
            audio=MP3_BYTES, characters=len(text), voice_id=voice_id, model_id=model_id
        )

    async def aclose(self) -> None:
        return None


def build_job(
    session: Session,
    pages: list[Page],
    *,
    ai_provider: str = AIProvider.NONE,
    ai_model: str = "",
    force: bool = False,
) -> int:
    job = GenerationJob(
        course_id=pages[0].course_id,
        status=JobStatus.QUEUED,
        ai_provider=ai_provider,
        ai_model=ai_model,
        voice_id="v1",
        voice_name="Testrøst",
        tts_model="eleven_multilingual_v2",
        voice_settings={"stability": 0.5},
        force_regenerate=force,
        total_items=len(pages),
    )
    session.add(job)
    session.flush()
    for page in pages:
        session.add(JobItem(job_id=job.id, page_id=page.id, status=ItemStatus.PENDING))
    session.commit()
    return job.id


@pytest.fixture()
def course_with_pages(db: Session) -> dict[str, Any]:
    course = make_course(db)
    module = make_module(db, course, "Modul 1 – Introduksjon", 1)
    html = "<h2>Velkommen</h2><p>Dette er <strong>emnet</strong>.</p><ul><li>Punkt én</li></ul>"
    page_a = make_page(
        db, course, module, "Introduksjon til kunstig intelligens", position=1, text=""
    )
    page_a.html = html
    page_a.original_text = html_to_text(html)
    from app.services.hashing import text_hash

    page_a.original_hash = text_hash(page_a.original_text)

    page_b = make_page(db, course, module, "Hva skal vi lære?", position=2, text="Vi lærer mye.")
    db.commit()
    db.refresh(page_a)
    db.refresh(page_b)
    return {"course": course, "module": module, "pages": [page_a, page_b]}


class TestHappyPath:
    async def test_full_run_writes_files_and_records_usage(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        pages = course_with_pages["pages"]
        job_id = build_job(db, pages)
        fake = FakeElevenLabs()

        await run_job(job_id, tts_client=fake)

        # Only clean text reached the "provider".
        assert len(fake.calls) == 2
        for call in fake.calls:
            assert "<" not in call["text"]
            assert call["voice_id"] == "v1"
            assert call["model_id"] == "eleven_multilingual_v2"

        with session_scope() as session:
            job = session.get(GenerationJob, job_id)
            assert job.status == JobStatus.COMPLETED
            assert job.completed_items == 2
            assert job.failed_items == 0
            assert all(item.status == ItemStatus.DONE for item in job.items)

            audio_rows = session.execute(select(GeneratedAudio)).scalars().all()
            assert len(audio_rows) == 2

            paths = sorted(row.relative_path for row in audio_rows)
            assert paths == [
                "mitt-emne/01-modul-1-introduksjon/01-introduksjon-til-kunstig-intelligens.mp3",
                "mitt-emne/01-modul-1-introduksjon/02-hva-skal-vi-laere.mp3",
            ]
            for row in audio_rows:
                on_disk = env / "output" / row.relative_path
                assert on_disk.is_file()
                assert on_disk.read_bytes() == MP3_BYTES
                assert row.voice_name == "Testrøst"
                # The original Canvas title survives even though the file name is a slug.
                assert row.original_title in {
                    "Introduksjon til kunstig intelligens",
                    "Hva skal vi lære?",
                }

            usage = session.execute(select(UsageRecord)).scalars().all()
            assert len(usage) == 2
            assert all(record.provider == "elevenlabs" for record in usage)
            assert all(record.estimated_cost_usd > 0 for record in usage)

    async def test_directory_layout_mirrors_canvas(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        job_id = build_job(db, course_with_pages["pages"])
        await run_job(job_id, tts_client=FakeElevenLabs())

        course_dir = env / "output" / "mitt-emne"
        assert course_dir.is_dir()
        module_dirs = sorted(p.name for p in course_dir.iterdir())
        assert module_dirs == ["01-modul-1-introduksjon"]

    async def test_generated_audio_is_served_by_the_api(
        self, env: Path, course_with_pages: dict[str, Any], db: Session, client
    ) -> None:
        page = course_with_pages["pages"][0]
        job_id = build_job(db, [page])
        await run_job(job_id, tts_client=FakeElevenLabs())

        response = client.get(f"/api/audio/{page.id}")
        assert response.status_code == 200
        assert response.content == MP3_BYTES


class TestChangeDetection:
    async def test_second_run_skips_unchanged_pages(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        pages = course_with_pages["pages"]
        await run_job(build_job(db, pages), tts_client=FakeElevenLabs())

        second = FakeElevenLabs()
        job_id = build_job(db, pages)
        await run_job(job_id, tts_client=second)

        assert second.calls == [], "no paid call may be made for unchanged pages"
        with session_scope() as session:
            job = session.get(GenerationJob, job_id)
            assert job.skipped_items == 2
            assert all(item.status == ItemStatus.SKIPPED for item in job.items)
            assert "allerede oppdatert" in job.items[0].message.lower()

    async def test_force_regenerate_runs_again(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        pages = course_with_pages["pages"]
        await run_job(build_job(db, pages), tts_client=FakeElevenLabs())

        second = FakeElevenLabs()
        await run_job(build_job(db, pages, force=True), tts_client=second)
        assert len(second.calls) == 2

    async def test_edited_text_triggers_a_new_call_with_the_edited_text(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        page = course_with_pages["pages"][1]
        await run_job(build_job(db, [page]), tts_client=FakeElevenLabs())

        with session_scope() as session:
            stored = session.get(Page, page.id)
            stored.processed_text = "Min egen innleste versjon."
            stored.manually_edited = True

        second = FakeElevenLabs()
        await run_job(build_job(db, [page]), tts_client=second)
        assert [c["text"] for c in second.calls] == ["Min egen innleste versjon."]

    async def test_reusing_a_filename_does_not_create_duplicates(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        page = course_with_pages["pages"][0]
        await run_job(build_job(db, [page]), tts_client=FakeElevenLabs())
        await run_job(build_job(db, [page], force=True), tts_client=FakeElevenLabs())

        files = list((env / "output" / "mitt-emne" / "01-modul-1-introduksjon").glob("*.mp3"))
        assert len(files) == 1


class TestAIStep:
    async def test_ai_output_is_what_gets_spoken(
        self, env: Path, course_with_pages: dict[str, Any], db: Session, monkeypatch
    ) -> None:
        from app.services import openai_service

        seen: list[str] = []

        async def fake_process(text: str, *, title: str, model: str, api_key: str, **_kw: Any):
            seen.append(text)
            return AIResult(
                text=f"Bearbeidet: {text}",
                input_tokens=100,
                output_tokens=120,
                model=model,
                provider="openai",
            )

        monkeypatch.setattr(openai_service, "process_text", fake_process)

        page = course_with_pages["pages"][1]
        job_id = build_job(db, [page], ai_provider=AIProvider.OPENAI, ai_model="gpt-5.6-terra")
        fake_tts = FakeElevenLabs()
        await run_job(job_id, tts_client=fake_tts)

        assert seen == ["Vi lærer mye."]
        assert fake_tts.calls[0]["text"] == "Bearbeidet: Vi lærer mye."

        with session_scope() as session:
            stored = session.get(Page, page.id)
            assert stored.processed_by == "openai"
            assert stored.processed_model == "gpt-5.6-terra"

            usage = session.execute(select(UsageRecord)).scalars().all()
            providers = sorted(record.provider for record in usage)
            assert providers == ["elevenlabs", "openai"]
            ai_usage = next(r for r in usage if r.provider == "openai")
            assert ai_usage.input_tokens == 100
            assert ai_usage.output_tokens == 120
            assert ai_usage.estimated_cost_usd > 0

    async def test_cached_ai_result_is_not_paid_for_twice(
        self, env: Path, course_with_pages: dict[str, Any], db: Session, monkeypatch
    ) -> None:
        from app.services import anthropic_service

        calls = {"n": 0}

        async def fake_process(text: str, *, title: str, model: str, api_key: str, **_kw: Any):
            calls["n"] += 1
            return AIResult(
                text="Bearbeidet tekst.",
                input_tokens=10,
                output_tokens=10,
                model=model,
                provider="anthropic",
            )

        monkeypatch.setattr(anthropic_service, "process_text", fake_process)

        page = course_with_pages["pages"][1]
        kwargs = {"ai_provider": AIProvider.ANTHROPIC, "ai_model": "claude-opus-5"}

        await run_job(build_job(db, [page], **kwargs), tts_client=FakeElevenLabs())
        await run_job(build_job(db, [page], force=True, **kwargs), tts_client=FakeElevenLabs())

        assert calls["n"] == 1, "the cached AI result must be reused on the second run"


class TestFailureHandling:
    async def test_one_failure_does_not_stop_the_queue(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        pages = course_with_pages["pages"]
        job_id = build_job(db, pages)
        fake = FakeElevenLabs(fail_on={"Vi lærer mye."})

        await run_job(job_id, tts_client=fake)

        with session_scope() as session:
            job = session.get(GenerationJob, job_id)
            assert job.status == JobStatus.COMPLETED_WITH_ERRORS
            assert job.completed_items == 1
            assert job.failed_items == 1

            failed = next(i for i in job.items if i.status == ItemStatus.FAILED)
            assert "500" in failed.message
            assert session.get(Page, failed.page_id).last_error

    async def test_a_page_without_text_fails_with_a_clear_message(
        self, env: Path, db: Session
    ) -> None:
        course = make_course(db)
        module = make_module(db, course, "Modul 1", 1)
        page = make_page(db, course, module, "Tom side", text="")
        page.html = ""
        page.original_text = ""
        page.original_hash = ""
        db.commit()

        job_id = build_job(db, [page])
        await run_job(job_id, tts_client=FakeElevenLabs())

        with session_scope() as session:
            item = session.execute(select(JobItem).where(JobItem.job_id == job_id)).scalar_one()
            assert item.status == ItemStatus.FAILED
            assert "ingen tekst" in item.message.lower()

    async def test_retry_after_a_failure_succeeds(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        page = course_with_pages["pages"][1]
        await run_job(build_job(db, [page]), tts_client=FakeElevenLabs(fail_on={"Vi lærer mye."}))

        job_id = build_job(db, [page])
        await run_job(job_id, tts_client=FakeElevenLabs())

        with session_scope() as session:
            item = session.execute(select(JobItem).where(JobItem.job_id == job_id)).scalar_one()
            assert item.status == ItemStatus.DONE
            assert session.get(Page, page.id).last_error == ""


class TestCancellation:
    async def test_cancelling_skips_the_remaining_pages(
        self, env: Path, course_with_pages: dict[str, Any], db: Session
    ) -> None:
        pages = course_with_pages["pages"]
        job_id = build_job(db, pages)
        fake = FakeElevenLabs()

        await run_job(job_id, is_cancelled=lambda: True, tts_client=fake)

        assert fake.calls == []
        with session_scope() as session:
            job = session.get(GenerationJob, job_id)
            assert job.status == JobStatus.CANCELLED
            assert all(item.status == ItemStatus.SKIPPED for item in job.items)


class TestAIPunctuation:
    async def test_ai_lines_get_endings_before_they_are_spoken(
        self, env: Path, course_with_pages: dict[str, Any], db: Session, monkeypatch
    ) -> None:
        from app.services import openai_service

        async def fake_process(text: str, *, title: str, model: str, api_key: str, **_kw: Any):
            return AIResult(
                text="Første del\nAndre del,",
                input_tokens=10,
                output_tokens=10,
                model=model,
                provider="openai",
            )

        monkeypatch.setattr(openai_service, "process_text", fake_process)
        page = course_with_pages["pages"][1]
        kwargs = {"ai_provider": AIProvider.OPENAI, "ai_model": "gpt-5.6-terra"}

        first = FakeElevenLabs()
        await run_job(build_job(db, [page], **kwargs), tts_client=first)
        assert [c["text"] for c in first.calls] == ["Første del.\nAndre del."]

        with session_scope() as session:
            assert session.get(Page, page.id).processed_text == "Første del.\nAndre del."

        # Stored text and spoken text are identical, so the next run neither
        # regenerates the audio nor pays for it again.
        second = FakeElevenLabs()
        await run_job(build_job(db, [page], **kwargs), tts_client=second)
        assert second.calls == []
