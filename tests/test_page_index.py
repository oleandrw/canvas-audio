"""The cheap page list behind the Canvas view.

Two things must hold: it must never drag the raw Canvas HTML out of the database
just to show a list of titles, and its status must be the same status the rest of
the application uses — a list that disagrees with the pipeline is worse than a
slow one.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models.core import Page, PageAudioStatus
from app.services import page_index
from app.services.hashing import page_audio_status
from tests.conftest import attach_audio, make_course, make_module, make_page


@pytest.fixture()
def course(db: Session) -> dict[str, Any]:
    """One course with every status represented, across two modules."""
    course = make_course(db)
    first = make_module(db, course, "Modul 1 – Introduksjon", 1)
    second = make_module(db, course, "Modul 2 – Vurdering", 2)

    ready = make_page(db, course, first, "Velkommen", position=1)
    ready.html = "<p>" + "Mye Canvas-HTML. " * 200 + "</p>"
    attach_audio(db, ready)

    changed = make_page(db, course, first, "Endret side", position=2)
    attach_audio(db, changed, stale=True)

    make_page(db, course, second, "Uten lyd", position=1)

    failed = make_page(db, course, second, "Feilet side", position=2)
    failed.last_error = "ElevenLabs svarte 500"

    loose = make_page(db, course, None, "Løs side", position=1)
    loose.title = "Løs side"

    db.commit()
    return {"course": course, "modules": [first, second]}


class TestCourseRows:
    def test_the_canvas_html_is_never_read(self, course: dict[str, Any], db: Session) -> None:
        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *_rest: Any) -> None:
            statements.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", record)
        try:
            rows = page_index.course_rows(db, course["course"].id)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", record)

        assert len(rows) == 5
        assert statements, "the listener saw nothing, so it proves nothing"
        assert not any("pages.html" in statement for statement in statements)

    def test_the_status_is_the_same_one_the_pipeline_uses(
        self, course: dict[str, Any], db: Session
    ) -> None:
        by_id = {row.id: row.status for row in page_index.course_rows(db, course["course"].id)}

        pages = db.execute(select(Page).where(Page.archived.is_(False))).scalars().all()
        assert {page_audio_status(page) for page in pages} == {
            PageAudioStatus.UP_TO_DATE,
            PageAudioStatus.TEXT_CHANGED,
            PageAudioStatus.NOT_GENERATED,
            PageAudioStatus.FAILED,
        }, "the fixture must exercise every status"
        for page in pages:
            assert by_id[page.id] == page_audio_status(page)

    def test_the_status_is_derived_once_per_page(
        self, course: dict[str, Any], db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        original = page_index.audio_status

        def counted(**kwargs: Any) -> str:
            calls.append(kwargs["tts_text"])
            return original(**kwargs)

        monkeypatch.setattr(page_index, "audio_status", counted)
        page_index.course_stats(db, course["course"].id)

        assert len(calls) == 5, "the old view hashed every page twice"

    def test_search_matches_titles_and_module_names(
        self, course: dict[str, Any], db: Session
    ) -> None:
        titles = [
            row.title for row in page_index.course_rows(db, course["course"].id, search="velk")
        ]
        assert titles == ["Velkommen"]

        by_module = page_index.course_rows(db, course["course"].id, search="vurdering")
        assert [row.title for row in by_module] == ["Uten lyd", "Feilet side"]

    def test_the_status_filter_narrows_the_list(self, course: dict[str, Any], db: Session) -> None:
        rows = page_index.course_rows(db, course["course"].id, status="endret")
        assert [row.title for row in rows] == ["Endret side"]

        rows = page_index.course_rows(db, course["course"].id, status="feilet")
        assert [row.title for row in rows] == ["Feilet side"]

    def test_one_module_at_a_time(self, course: dict[str, Any], db: Session) -> None:
        rows = page_index.course_rows(db, course["course"].id, module_id=course["modules"][1].id)
        assert [row.title for row in rows] == ["Uten lyd", "Feilet side"]

        loose = page_index.course_rows(db, course["course"].id, module_id=page_index.NO_MODULE)
        assert [row.title for row in loose] == ["Løs side"]

    def test_pages_without_a_module_come_last(self, course: dict[str, Any], db: Session) -> None:
        rows = page_index.course_rows(db, course["course"].id)
        assert rows[-1].title == "Løs side"
        assert rows[-1].module_name == "Uten modul"


class TestSummaries:
    def test_the_counts_add_up(self, course: dict[str, Any], db: Session) -> None:
        summaries = page_index.module_summaries(db, course["course"].id)

        assert [(s.name, s.pages) for s in summaries] == [
            ("Modul 1 – Introduksjon", 2),
            ("Modul 2 – Vurdering", 2),
            ("Uten modul", 1),
        ]
        first, second, loose = summaries
        assert (first.ready, first.changed) == (1, 1)
        assert (second.missing, second.failed) == (1, 1)
        assert loose.missing == 1

    def test_an_empty_module_is_still_listed(self, course: dict[str, Any], db: Session) -> None:
        make_module(db, course["course"], "Modul 3 – Tom", 3)
        db.commit()

        names = [s.name for s in page_index.module_summaries(db, course["course"].id)]
        assert "Modul 3 – Tom" in names

    def test_the_course_totals(self, course: dict[str, Any], db: Session) -> None:
        stats = page_index.course_stats(db, course["course"].id)

        assert stats["modules"] == 2
        assert stats["pages"] == 5
        assert stats["generated"] == 2
        assert stats["not_generated"] == 2
        assert stats["changed"] == 1
        assert stats["failed"] == 1
        assert stats["remaining"] == 3
        assert stats["total_duration_label"] and stats["total_size_label"]
