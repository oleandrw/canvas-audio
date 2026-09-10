"""Canvas HTML → speakable text."""

from __future__ import annotations

import pytest

from app.services.errors import InvalidContentError
from app.services.text_service import (
    contains_html,
    ensure_plain_text,
    html_to_text,
    summarize_structure,
)


class TestCleaning:
    def test_removes_scripts_and_styles(self) -> None:
        html = """
        <div>
          <script>alert('xss')</script>
          <style>.a { color: red }</style>
          <p>Faktisk innhold.</p>
        </div>
        """
        text = html_to_text(html)
        assert "alert" not in text
        assert "color" not in text
        assert text == "Faktisk innhold."

    def test_removes_technical_elements(self) -> None:
        html = """
        <p>Tekst.</p>
        <iframe src="https://example.com"></iframe>
        <form><button>Send</button><input value="x"></form>
        <img src="bilde.png" alt="Et bilde">
        """
        text = html_to_text(html)
        assert "Tekst." in text
        assert "Send" not in text
        assert "bilde" not in text.lower()

    def test_strips_html_comments(self) -> None:
        assert "skjult" not in html_to_text("<p>Synlig.</p><!-- skjult kommentar -->")

    def test_headings_become_their_own_sentence(self) -> None:
        text = html_to_text("<h2>Læringsmål</h2><p>Du skal kunne mye.</p>")
        assert text.splitlines()[0] == "Læringsmål."
        assert "Du skal kunne mye." in text

    def test_unordered_list_items_become_sentences(self) -> None:
        html = "<ul><li>Første punkt</li><li>Andre punkt</li></ul>"
        lines = [line for line in html_to_text(html).splitlines() if line]
        assert lines == ["Første punkt.", "Andre punkt."]

    def test_ordered_list_keeps_numbering(self) -> None:
        html = "<ol><li>Start her</li><li>Så her</li></ol>"
        lines = [line for line in html_to_text(html).splitlines() if line]
        assert lines == ["1. Start her.", "2. Så her."]

    def test_tables_are_read_row_by_row(self) -> None:
        html = """
        <table>
          <tr><th>Begrep</th><th>Forklaring</th></tr>
          <tr><td>KI</td><td>Kunstig intelligens</td></tr>
        </table>
        """
        lines = [line for line in html_to_text(html).splitlines() if line]
        assert lines == ["Begrep, Forklaring.", "KI, Kunstig intelligens."]

    def test_link_text_is_kept_but_url_is_dropped(self) -> None:
        html = '<p>Les mer i <a href="https://example.com/doc">kursplanen</a> før du starter.</p>'
        text = html_to_text(html)
        assert "kursplanen" in text
        assert "example.com" not in text

    def test_bare_url_links_are_removed(self) -> None:
        html = '<p>Kilde: <a href="https://example.com">https://example.com</a></p>'
        text = html_to_text(html)
        assert "https" not in text
        assert "Kilde:" in text

    def test_collapses_excess_whitespace(self) -> None:
        html = "<p>Mye    mellomrom\n\n\n  her.</p>"
        assert html_to_text(html) == "Mye mellomrom her."
        assert "\n\n\n" not in html_to_text("<p>a</p><p>b</p><p>c</p>")

    def test_inline_markup_does_not_split_sentences(self) -> None:
        html = "<p>Dette er <strong>viktig</strong> og <em>nyttig</em>.</p>"
        assert html_to_text(html) == "Dette er viktig og nyttig."

    def test_handles_malformed_html_without_raising(self) -> None:
        text = html_to_text("<p>Ustengt avsnitt <div>og en div<span>uten slutt")
        assert "Ustengt avsnitt" in text

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_input_returns_empty_string(self, value: str | None) -> None:
        assert html_to_text(value) == ""


class TestGuards:
    def test_detects_leftover_markup(self) -> None:
        assert contains_html("<p>hei</p>")
        assert not contains_html("helt vanlig tekst med < og > tegn")

    def test_ensure_plain_text_rejects_html(self) -> None:
        with pytest.raises(InvalidContentError, match="HTML"):
            ensure_plain_text("<p>ikke renset</p>")

    def test_ensure_plain_text_rejects_empty(self) -> None:
        with pytest.raises(InvalidContentError):
            ensure_plain_text("   ")

    def test_ensure_plain_text_returns_trimmed_text(self) -> None:
        assert ensure_plain_text("  hei  ") == "hei"


def test_structure_summary_counts_elements() -> None:
    html = "<h1>T</h1><p>a</p><p>b</p><ul><li>x</li></ul><table><tr><td>c</td></tr></table>"
    summary = summarize_structure(html)
    assert summary["headings"] == 1
    assert summary["paragraphs"] == 2
    assert summary["lists"] == 1
    assert summary["tables"] == 1
