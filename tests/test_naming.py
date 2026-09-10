"""Filename safety: the layer between free-text Canvas titles and the filesystem."""

from __future__ import annotations

import pytest

from app.services.naming import (
    audio_filename,
    course_dirname,
    module_dirname,
    numbered_prefix,
    sanitize_filename,
    slugify,
    unique_name,
)


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Introduksjon til kunstig intelligens", "introduksjon-til-kunstig-intelligens"),
            ("Hva skal vi lære?", "hva-skal-vi-laere"),
            ("Ærlig ønske om åpenhet", "aerlig-oenske-om-aapenhet"),
            ("Modul 1 – Introduksjon", "modul-1-introduksjon"),
            ("  spaces   everywhere  ", "spaces-everywhere"),
            ("Émile & Co.", "emile-co"),
            ("///", "uten-navn"),
            ("", "uten-navn"),
        ],
    )
    def test_produces_ascii_slug(self, raw: str, expected: str) -> None:
        assert slugify(raw) == expected

    def test_truncates_long_input(self) -> None:
        slug = slugify("a" * 500, max_length=40)
        assert len(slug) <= 40
        assert not slug.endswith("-")

    def test_reserved_device_name_falls_back(self) -> None:
        assert slugify("CON") == "uten-navn"
        assert slugify("lpt1") == "uten-navn"


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Introduksjon til KI", "Introduksjon til KI"),
            ("Del 1: oversikt", "Del 1 oversikt"),
            ('bad"chars<>|?*', "badchars"),
            ("trailing dots...", "trailing dots"),
            ("   ", "side"),
        ],
    )
    def test_removes_illegal_characters(self, raw: str, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    @pytest.mark.parametrize(
        "attack",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32",
            "/absolute/path",
            "C:\\Windows\\evil",
        ],
    )
    def test_blocks_path_traversal(self, attack: str) -> None:
        result = sanitize_filename(attack)
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result

    def test_enforces_length_limit(self) -> None:
        assert len(sanitize_filename("x" * 400, max_length=120)) <= 120

    def test_rejects_windows_reserved_names(self) -> None:
        assert sanitize_filename("NUL") == "side"
        assert sanitize_filename("com4.mp3") == "side"


class TestUniqueName:
    def test_returns_candidate_when_free(self) -> None:
        assert unique_name("velkommen", set(), suffix=".mp3") == "velkommen"

    def test_appends_counter_on_collision(self) -> None:
        taken = {"velkommen.mp3", "velkommen-2.mp3"}
        assert unique_name("velkommen", taken, suffix=".mp3") == "velkommen-3"

    def test_collision_check_is_case_insensitive(self) -> None:
        assert unique_name("Velkommen", {"velkommen.mp3"}, suffix=".mp3") == "Velkommen-2"


class TestAudioFilename:
    def test_slug_style_is_default(self) -> None:
        assert (
            audio_filename("Introduksjon til kunstig intelligens", 1)
            == "01-introduksjon-til-kunstig-intelligens.mp3"
        )

    def test_title_style_keeps_original_title(self) -> None:
        assert (
            audio_filename("Introduksjon til kunstig intelligens", 1, style="title")
            == "01 - Introduksjon til kunstig intelligens.mp3"
        )

    def test_position_is_zero_padded(self) -> None:
        assert numbered_prefix(3) == "03"
        assert numbered_prefix(12) == "12"
        assert audio_filename("Side", 7).startswith("07-")


def test_directory_names_keep_canvas_order() -> None:
    assert module_dirname("Modul 1 – Introduksjon", 1) == "01-modul-1-introduksjon"
    assert module_dirname("Tema 1", 2) == "02-tema-1"
    assert course_dirname("Mitt emne") == "mitt-emne"
