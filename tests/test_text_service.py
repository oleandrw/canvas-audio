"""Canvas HTML → speakable text."""

from __future__ import annotations

import pytest

from app.services.errors import InvalidContentError
from app.services.text_service import (
    contains_html,
    end_sentence,
    ensure_plain_text,
    ensure_sentence_endings,
    html_to_text,
    split_for_tts,
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


def lines(html: str) -> list[str]:
    return [line for line in html_to_text(html).splitlines() if line]


class TestPausesForNarration:
    """Every block a listener hears as separate must end with a sentence mark."""

    def test_list_items_with_a_trailing_line_break_get_a_full_stop(self) -> None:
        # The pattern the Canvas editor produces, taken from real course pages.
        html = (
            "<ul><li>Oppgi kilder til bilder på siste slide<br></li>"
            "<li>Tydelig bruk av zoom funksjonen (ikke autozoom)<br></li></ul>"
        )
        assert lines(html) == [
            "Oppgi kilder til bilder på siste slide.",
            "Tydelig bruk av zoom funksjonen (ikke autozoom).",
        ]

    def test_every_list_item_ends_with_a_full_stop(self) -> None:
        html = (
            "<ul><li>Første punkt</li><li><span>Andre punkt</span></li><li><p>Tredje</p></li></ul>"
        )
        assert lines(html) == ["Første punkt.", "Andre punkt.", "Tredje."]

    def test_ordered_list_items_end_with_a_full_stop(self) -> None:
        assert lines("<ol><li>Logg inn</li><li>Åpne emnet</li></ol>") == [
            "1. Logg inn.",
            "2. Åpne emnet.",
        ]

    def test_text_before_a_nested_list_ends_its_sentence(self) -> None:
        html = "<ul><li>Del 1<ul><li>underpunkt</li></ul></li></ul>"
        assert lines(html) == ["Del 1. underpunkt."]

    def test_text_right_before_a_block_ends_its_sentence(self) -> None:
        assert lines("<div>Innledning<p>Avsnitt</p></div>") == ["Innledning.", "Avsnitt."]

    def test_lines_split_by_line_breaks_each_end_a_sentence(self) -> None:
        html = "<p>Lysbilde 3 - Animasjoner og link<br>Lysbilde 4 - Objekter<br>Lysbilde 5</p>"
        assert lines(html) == [
            "Lysbilde 3 - Animasjoner og link.",
            "Lysbilde 4 - Objekter.",
            "Lysbilde 5.",
        ]

    def test_hand_typed_bullets_are_dropped_and_each_line_ends(self) -> None:
        html = "<p>• epler<br>• pærer<br>- bananer</p>"
        assert lines(html) == ["epler.", "pærer.", "bananer."]

    def test_a_trailing_comma_or_semicolon_becomes_a_full_stop(self) -> None:
        html = "<p>Beste hilsener,</p><ul><li>epler;</li></ul>"
        assert lines(html) == ["Beste hilsener.", "epler."]

    def test_existing_sentence_marks_are_kept(self) -> None:
        html = "<ul><li>Hva er KI?</li><li>Viktig!</li><li>Du trenger:</li><li>«Hei.»</li></ul>"
        assert lines(html) == ["Hva er KI?", "Viktig!", "Du trenger:", "«Hei.»"]

    def test_a_colon_before_a_list_is_kept_and_not_merged_into_it(self) -> None:
        assert lines("Du trenger:<br><ul><li>epler</li></ul>") == ["Du trenger:", "epler."]

    def test_headings_and_table_rows_end_a_sentence(self) -> None:
        html = "<h2>Oversikt</h2><table><tr><td>KI</td><td>Kunstig intelligens</td></tr></table>"
        assert lines(html) == ["Oversikt.", "KI, Kunstig intelligens."]


class TestLowerCaseLines:
    def test_a_lower_case_list_typed_one_item_per_line_keeps_its_pauses(self) -> None:
        # From a real course page: each line is a separate item, not one sentence.
        html = (
            "<p>opplysninger om helse og helserelaterte forhold<br>"
            "genetiske opplysninger<br>"
            "opplysninger om fagforeningsmedlemskap.</p>"
        )
        assert lines(html) == [
            "opplysninger om helse og helserelaterte forhold.",
            "genetiske opplysninger.",
            "opplysninger om fagforeningsmedlemskap.",
        ]

    @pytest.mark.parametrize("word", ["og", "som", "at", "i", "med", "and", "the"])
    def test_a_line_ending_in_a_function_word_is_joined(self, word: str) -> None:
        html = f"<p>Dette er starten {word}<br>fortsettelsen av setningen.</p>"
        assert lines(html) == [f"Dette er starten {word} fortsettelsen av setningen."]


class TestSentenceEndingHelpers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Punkt", "Punkt."),
            ("Punkt.", "Punkt."),
            ("Punkt,", "Punkt."),
            ("Punkt;", "Punkt."),
            ("Hva?", "Hva?"),
            ("Obs!", "Obs!"),
            ("Følgende:", "Følgende:"),
            ("Se side 4 (valgfritt)", "Se side 4 (valgfritt)."),
            # From a real course page: the "." in "osv." does not end the sentence.
            ("Lovgrunnlag (Arbeidsmiljøloven, osv.)", "Lovgrunnlag (Arbeidsmiljøloven, osv.)."),
            ("«Hei.»", "«Hei.»"),
            ('Hva nå?"', 'Hva nå?"'),
            ("Og så videre...", "Og så videre..."),
        ],
    )
    def test_end_sentence(self, text: str, expected: str) -> None:
        assert end_sentence(text) == expected

    def test_ai_text_gets_an_ending_on_every_line(self) -> None:
        text = "Første punkt\n\nAndre punkt,\n- Tredje punkt\nFerdig."
        assert ensure_sentence_endings(text) == (
            "Første punkt.\n\nAndre punkt.\nTredje punkt.\nFerdig."
        )

    def test_is_idempotent(self) -> None:
        once = ensure_sentence_endings("• Linje en\nLinje to,\n\nSpørsmål?")
        assert ensure_sentence_endings(once) == once


class TestSplittingForTts:
    """A page longer than the model's limit is read in several takes."""

    def test_short_text_is_left_whole(self) -> None:
        assert split_for_tts("Kort tekst.", 100) == ["Kort tekst."]
        assert split_for_tts("  ", 100) == []

    def test_it_never_exceeds_the_budget(self) -> None:
        text = "\n\n".join(f"Avsnitt {i} med litt tekst i seg." for i in range(40))
        chunks = split_for_tts(text, 120)
        assert len(chunks) > 1
        assert all(len(chunk) <= 120 for chunk in chunks)

    def test_it_cuts_between_sentences_not_inside_them(self) -> None:
        text = "Første setning her. Andre setning her. Tredje setning her."
        chunks = split_for_tts(text, 25)
        assert chunks == ["Første setning her.", "Andre setning her.", "Tredje setning her."]

    def test_it_fills_the_budget_before_it_cuts(self) -> None:
        """Fewer requests is both cheaper and steadier in tone."""
        text = "Første setning her. Andre setning her. Tredje setning her."
        assert split_for_tts(text, 40) == [
            "Første setning her. Andre setning her.",
            "Tredje setning her.",
        ]

    def test_nothing_is_lost_and_the_order_is_kept(self) -> None:
        text = "\n\n".join(f"Avsnitt {i}. Med to setninger." for i in range(25))
        joined = " ".join(" ".join(chunk.split()) for chunk in split_for_tts(text, 90))
        assert joined == " ".join(text.split())

    def test_one_enormous_sentence_is_cut_on_a_space(self) -> None:
        text = "ord " * 200  # no sentence end anywhere
        chunks = split_for_tts(text.strip(), 50)
        assert all(len(chunk) <= 50 for chunk in chunks)
        assert " ".join(chunks).split() == text.split()

    def test_a_single_word_longer_than_the_budget_is_still_split(self) -> None:
        chunks = split_for_tts("a" * 120, 50)
        assert [len(chunk) for chunk in chunks] == [50, 50, 20]
