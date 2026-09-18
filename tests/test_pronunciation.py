"""The user's pronunciation list.

The dangerous part is not the replacing — it is that the text which is read aloud
must be exactly the text the application hashes. If those two ever drift apart,
every page shows up as changed and the whole course gets regenerated at full
price. The tests below pin that down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import session_scope
from app.models.core import Page, PageAudioStatus, PronunciationRule
from app.services import pronunciation
from app.services.hashing import page_audio_status, text_hash
from app.services.pronunciation import Rule, apply_rules
from tests.conftest import attach_audio, make_course, make_module, make_page


class TestReplacing:
    def test_it_replaces_whole_words_regardless_of_case(self) -> None:
        rules = [Rule("HiØ", "Høgskolen i Østfold")]
        assert apply_rules("Velkommen til HiØ.", rules) == "Velkommen til Høgskolen i Østfold."
        assert apply_rules("hiø er her", rules) == "Høgskolen i Østfold er her"

    def test_it_does_not_replace_inside_a_longer_word(self) -> None:
        assert apply_rules("Uhiøbar tekst", [Rule("hiø", "X")]) == "Uhiøbar tekst"

    def test_case_can_be_made_to_matter(self) -> None:
        rules = [Rule("HiØ", "Høgskolen", match_case=True)]
        assert apply_rules("HIØ og HiØ", rules) == "HIØ og Høgskolen"

    def test_the_longest_pattern_wins(self) -> None:
        """The replacements differ, so a shortest-first order would show up here."""
        rules = [Rule("NTNU", "N T N U"), Rule("NTNU Gjøvik", "campus Gjøvik")]
        assert apply_rules("NTNU Gjøvik og NTNU", rules) == "campus Gjøvik og N T N U"

    def test_a_replacement_is_never_replaced_again(self) -> None:
        """Otherwise «a → b» and «b → c» would quietly turn every a into c."""
        rules = [Rule("a", "b"), Rule("b", "c")]
        assert apply_rules("a b", rules) == "b c"

    def test_a_symbol_has_no_word_boundary_to_respect(self) -> None:
        assert apply_rules("Se § 4", [Rule("§", "paragraf")]) == "Se paragraf 4"

    def test_an_abbreviation_keeps_its_leading_boundary(self) -> None:
        assert apply_rules("jf. loven", [Rule("jf.", "jamfør")]) == "jamfør loven"
        assert apply_rules("ajf. loven", [Rule("jf.", "jamfør")]) == "ajf. loven"

    def test_an_empty_list_changes_nothing(self) -> None:
        assert apply_rules("Uendret tekst.", []) == "Uendret tekst."

    def test_a_disabled_rule_is_not_loaded(self, db: Session) -> None:
        db.add(PronunciationRule(pattern="HiØ", replacement="Høgskolen", enabled=False))
        db.add(PronunciationRule(pattern="jf.", replacement="jamfør", enabled=True))
        db.commit()

        assert [rule.pattern for rule in pronunciation.load_rules(db)] == ["jf."]

    def test_regex_characters_are_literal(self) -> None:
        """A pattern is text the user typed, never a pattern language."""
        rules = [Rule("2.50", "to femti", whole_word=False)]
        assert apply_rules("koster 2.50 kr", rules) == "koster to femti kr"
        assert apply_rules("2x50 kr", rules) == "2x50 kr", "the dot is a dot, not «any character»"

    def test_a_pattern_that_is_not_valid_regex_still_works(self) -> None:
        """«(jf.» would be a broken pattern if patterns were regex."""
        rules = [
            Rule("(jf.", "jamfør", whole_word=False),
            Rule("[§]", "paragraf", whole_word=False),
        ]
        assert apply_rules("Se (jf. og [§] her", rules) == "Se jamfør og paragraf her"


class TestAppliedToPages:
    @pytest.fixture()
    def course(self, db: Session, env: Path) -> dict[str, Any]:
        course = make_course(db)
        module = make_module(db, course, "Modul 1", 1)
        page = make_page(db, course, module, "Om HiØ", text="")
        page.html = "<p>Velkommen til HiØ. Se jf. reglene.</p>"
        page.original_text = "Velkommen til HiØ. Se jf. reglene."
        page.original_hash = text_hash(page.original_text)
        attach_audio(db, page)
        db.commit()
        return {"course": course, "page_id": page.id}

    def test_the_stored_text_is_the_text_that_will_be_read(
        self, course: dict[str, Any], db: Session
    ) -> None:
        db.add(PronunciationRule(pattern="HiØ", replacement="Høgskolen i Østfold", position=1))
        db.commit()

        from app.services.sync_service import reclean_course

        changed = reclean_course(db, course["course"].id)
        db.commit()

        page = db.get(Page, course["page_id"])
        assert changed == 1
        assert "Høgskolen i Østfold" in page.original_text
        assert "HiØ" not in page.tts_text
        # The hash follows the text, so the status is honest about the change.
        assert page.original_hash == text_hash(page.original_text)
        assert page_audio_status(page) == PageAudioStatus.CANVAS_CHANGED

    def test_a_list_that_matches_nothing_leaves_the_pages_alone(
        self, course: dict[str, Any], db: Session
    ) -> None:
        db.add(PronunciationRule(pattern="finnes-ikke", replacement="noe", position=1))
        db.commit()

        from app.services.sync_service import reclean_course

        before = db.get(Page, course["page_id"]).original_hash
        changed = reclean_course(db, course["course"].id)
        db.commit()

        assert changed == 0
        page = db.get(Page, course["page_id"])
        assert page.original_hash == before
        assert page_audio_status(page) == PageAudioStatus.UP_TO_DATE, "no needless regeneration"

    async def test_a_synced_page_arrives_already_replaced(self, env: Path, db: Session) -> None:
        from app.services.sync_service import sync_course
        from tests.test_sync_service import canvas_transport

        db.add(PronunciationRule(pattern="KI", replacement="kunstig intelligens", position=1))
        db.commit()

        await sync_course(
            base_url="https://canvas.example.test",
            token="12345~token",
            canvas_course_id="4711",
            transport=canvas_transport(),
        )

        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Om emnet"))
            assert "kunstig intelligens" in page.original_text
            assert page.original_hash == text_hash(page.original_text)


class TestRoutes:
    def test_saving_the_list_recleans_the_pages(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Velkommen"))
            page.html = "<p>Velkommen til HiØ.</p>"
            page.original_text = "Velkommen til HiØ."
            page.original_hash = text_hash(page.original_text)

        response = client.put(
            "/api/uttale",
            json={"rules": [{"pattern": "HiØ", "replacement": "Høgskolen i Østfold"}]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["changed_pages"] == 1
        assert "1 sider" in body["message"]
        with session_scope() as session:
            page = session.scalar(select(Page).where(Page.title == "Velkommen"))
            assert page.original_text == "Velkommen til Høgskolen i Østfold."

    def test_the_list_comes_back_in_order(self, client: TestClient) -> None:
        client.put(
            "/api/uttale",
            json={
                "rules": [
                    {"pattern": "b", "replacement": "bee"},
                    {"pattern": "a", "replacement": "aa", "whole_word": False},
                ]
            },
        )

        rows = client.get("/api/uttale").json()
        assert [row["pattern"] for row in rows] == ["b", "a"]
        assert rows[1]["whole_word"] is False

    def test_saving_replaces_the_whole_list(self, client: TestClient) -> None:
        client.put("/api/uttale", json={"rules": [{"pattern": "a", "replacement": "aa"}]})
        client.put("/api/uttale", json={"rules": [{"pattern": "b", "replacement": "bb"}]})

        rows = client.get("/api/uttale").json()
        assert [row["pattern"] for row in rows] == ["b"]

    def test_the_sample_shows_what_will_be_read(self, client: TestClient) -> None:
        client.put("/api/uttale", json={"rules": [{"pattern": "HiØ", "replacement": "Høgskolen"}]})

        response = client.post("/api/uttale/prov", json={"text": "Hei fra HiØ."})

        assert response.status_code == 200
        assert response.json()["text"] == "Hei fra Høgskolen."

    def test_an_empty_pattern_is_rejected(self, client: TestClient) -> None:
        response = client.put("/api/uttale", json={"rules": [{"pattern": "   "}]})
        assert response.status_code == 422

    def test_an_absurdly_long_pattern_is_rejected(self, client: TestClient) -> None:
        response = client.put("/api/uttale", json={"rules": [{"pattern": "a" * 201}]})
        assert response.status_code == 422

    def test_the_settings_page_shows_the_list(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        client.put("/api/uttale", json={"rules": [{"pattern": "HiØ", "replacement": "Høgskolen"}]})

        page = client.get("/innstillinger").text

        assert "Uttale" in page
        assert 'value="HiØ"' in page
        assert 'value="Høgskolen"' in page

    def test_nothing_is_fetched_from_canvas_when_saving(
        self, client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saving the list is local text work; it must never cost a Canvas call."""

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("no Canvas request may be made")

        monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
        response = client.put(
            "/api/uttale", json={"rules": [{"pattern": "HiØ", "replacement": "Høgskolen"}]}
        )
        assert response.status_code == 200


class TestWithAI:
    """The AI rewrites the text, so its output goes through the list as well."""

    async def test_the_ai_output_is_replaced_and_stored_as_spoken(
        self, env: Path, db: Session
    ) -> None:
        from app.jobs import pipeline
        from app.models.core import AIProvider
        from app.services.ai_common import AIResult
        from tests.test_pipeline import FakeElevenLabs, build_job

        course = make_course(db)
        module = make_module(db, course, "Modul 1", 1)
        page = make_page(db, course, module, "Om studiet", text="Noe tekst.")
        db.add(PronunciationRule(pattern="HiØ", replacement="Høgskolen i Østfold", position=1))
        db.commit()

        async def fake_ai(*_args: Any, **_kwargs: Any) -> AIResult:
            return AIResult(
                text="Velkommen til HiØ",
                provider="openai",
                model="gpt-test",
                input_tokens=10,
                output_tokens=10,
            )

        fake_tts = FakeElevenLabs()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pipeline, "_run_ai", fake_ai)
            job_id = build_job(db, [page], ai_provider=AIProvider.OPENAI, ai_model="gpt-test")
            await pipeline.run_job(job_id, tts_client=fake_tts)

        spoken = fake_tts.calls[0]["text"]
        assert spoken == "Velkommen til Høgskolen i Østfold."
        with session_scope() as session:
            stored = session.get(Page, page.id)
            assert stored.processed_text == spoken, "stored and spoken must be the same text"
            assert stored.audio is not None
            assert stored.audio.source_text_hash == text_hash(spoken)
