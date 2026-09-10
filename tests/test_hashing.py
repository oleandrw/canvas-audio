"""Change detection — the logic that decides whether money needs to be spent."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.core import AIProvider, PageAudioStatus
from app.services.hashing import (
    ai_result_is_reusable,
    audio_is_current,
    page_audio_status,
    status_label,
    text_hash,
)
from app.services.prompts import PROMPT_VERSION
from tests.conftest import attach_audio, make_course, make_module, make_page


class TestTextHash:
    def test_is_stable_for_identical_text(self) -> None:
        assert text_hash("Hei verden") == text_hash("Hei verden")

    def test_ignores_trailing_whitespace(self) -> None:
        assert text_hash("linje  \nto  ") == text_hash("linje\nto")

    def test_changes_when_content_changes(self) -> None:
        assert text_hash("Hei") != text_hash("Hei!")


class TestPageStatus:
    def _page(self, db: Session, **kwargs: Any) -> Any:
        course = make_course(db)
        module = make_module(db, course, "Modul 1", 1)
        return make_page(db, course, module, "Velkommen", **kwargs)

    def test_no_audio_means_not_generated(self, db: Session) -> None:
        page = self._page(db)
        assert page_audio_status(page) == PageAudioStatus.NOT_GENERATED
        assert not audio_is_current(page)

    def test_no_audio_with_error_means_failed(self, db: Session) -> None:
        page = self._page(db)
        page.last_error = "ElevenLabs svarte 402"
        assert page_audio_status(page) == PageAudioStatus.FAILED

    def test_fresh_audio_is_up_to_date(self, db: Session) -> None:
        page = self._page(db)
        attach_audio(db, page)
        db.refresh(page)
        assert page_audio_status(page) == PageAudioStatus.UP_TO_DATE
        assert audio_is_current(page)

    def test_canvas_change_is_detected(self, db: Session) -> None:
        page = self._page(db)
        attach_audio(db, page)
        db.refresh(page)

        page.original_text = "Helt ny tekst fra Canvas."
        page.original_hash = text_hash(page.original_text)
        assert page_audio_status(page) == PageAudioStatus.CANVAS_CHANGED
        assert not audio_is_current(page)

    def test_manual_text_change_is_detected(self, db: Session) -> None:
        page = self._page(db)
        attach_audio(db, page)
        db.refresh(page)

        page.processed_text = "Manuelt omskrevet tekst."
        page.manually_edited = True
        assert page_audio_status(page) == PageAudioStatus.TEXT_CHANGED
        assert not audio_is_current(page)

    def test_all_statuses_have_a_norwegian_label(self) -> None:
        for status in (
            PageAudioStatus.UP_TO_DATE,
            PageAudioStatus.NOT_GENERATED,
            PageAudioStatus.CANVAS_CHANGED,
            PageAudioStatus.TEXT_CHANGED,
            PageAudioStatus.FAILED,
        ):
            icon, label, kind = status_label(status)
            assert icon and label and kind


class TestAIReuse:
    def _page(self, db: Session) -> Any:
        course = make_course(db)
        module = make_module(db, course, "Modul 1", 1)
        return make_page(db, course, module, "Velkommen")

    def test_no_ai_selected_never_needs_a_call(self, db: Session) -> None:
        page = self._page(db)
        assert ai_result_is_reusable(page, AIProvider.NONE, "")

    def test_missing_result_needs_a_call(self, db: Session) -> None:
        page = self._page(db)
        assert not ai_result_is_reusable(page, AIProvider.OPENAI, "gpt-5.6-terra")

    def test_matching_result_is_reused(self, db: Session) -> None:
        page = self._page(db)
        page.processed_text = "Bearbeidet."
        page.processed_by = AIProvider.OPENAI
        page.processed_model = "gpt-5.6-terra"
        page.processed_source_hash = page.original_hash
        page.prompt_version = PROMPT_VERSION
        assert ai_result_is_reusable(page, AIProvider.OPENAI, "gpt-5.6-terra")

    def test_different_model_needs_a_new_call(self, db: Session) -> None:
        page = self._page(db)
        page.processed_text = "Bearbeidet."
        page.processed_by = AIProvider.OPENAI
        page.processed_model = "gpt-5.6-terra"
        page.processed_source_hash = page.original_hash
        page.prompt_version = PROMPT_VERSION
        assert not ai_result_is_reusable(page, AIProvider.OPENAI, "gpt-5.6-luna")

    def test_changed_source_needs_a_new_call(self, db: Session) -> None:
        page = self._page(db)
        page.processed_text = "Bearbeidet."
        page.processed_by = AIProvider.ANTHROPIC
        page.processed_model = "claude-opus-5"
        page.processed_source_hash = "an-old-hash"
        page.prompt_version = PROMPT_VERSION
        assert not ai_result_is_reusable(page, AIProvider.ANTHROPIC, "claude-opus-5")

    def test_new_prompt_version_invalidates_the_cache(self, db: Session) -> None:
        page = self._page(db)
        page.processed_text = "Bearbeidet."
        page.processed_by = AIProvider.ANTHROPIC
        page.processed_model = "claude-opus-5"
        page.processed_source_hash = page.original_hash
        page.prompt_version = "0"
        assert not ai_result_is_reusable(page, AIProvider.ANTHROPIC, "claude-opus-5")

    def test_manual_edits_are_never_overwritten(self, db: Session) -> None:
        page = self._page(db)
        page.manually_edited = True
        assert ai_result_is_reusable(page, AIProvider.OPENAI, "gpt-5.6-terra")
