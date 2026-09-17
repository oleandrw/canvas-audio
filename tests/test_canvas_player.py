"""Canvas player: the HTML block, splicing it into page bodies, and narration."""

from __future__ import annotations

import pytest

from app.services import canvas_player as cp
from app.services.text_service import html_to_text

SRC = "/courses/4711/files/99/download"
CONTENT = (
    "<h2>Om RAG</h2>\n"
    '<div class="intro"><p>Du har <strong>allerede</strong> erfaring.</p></div>\n'
    "<p>Neste avsnitt.</p>"
)


class TestPlayerHtml:
    def test_the_default_matches_the_original_design(self) -> None:
        block = cp.player_html(cp.PlayerDesign(), SRC)
        for fragment in (
            "background: #f7faff",
            "border: 1px solid #dfe9f5",
            "background: #eaf2fd",
            "color: #0b5bd3",
            "Lytt til denne siden",
            "🎧",
            f'src="{SRC}"',
            'type="audio/mpeg"',
        ):
            assert fragment in block
        assert block.startswith(f'<div class="{cp.MARKER_CLASS}" {cp.MARKER_ATTR}="1"')

    @pytest.mark.parametrize("style", list(cp.STYLES))
    def test_every_style_is_one_marked_block_with_the_audio(self, style: str) -> None:
        block = cp.player_html(cp.PlayerDesign(style=style), SRC)
        assert cp.find_player_blocks(block) == [(0, len(block))]
        assert block.count("<audio") == 1

    def test_user_text_is_escaped(self) -> None:
        block = cp.player_html(cp.PlayerDesign(label='<script>alert(1)</script> "hei"'), SRC)
        assert "<script>" not in block
        assert "&lt;script&gt;" in block

    def test_invalid_choices_fall_back_to_the_defaults(self) -> None:
        design = cp.PlayerDesign(
            style="blink", color="red; background:url(x)", label="   ", icon="💣"
        ).normalized()
        assert design == cp.PlayerDesign()

    def test_a_custom_colour_gets_matching_tints(self) -> None:
        block = cp.player_html(cp.PlayerDesign(color="#2e7d32"), SRC)
        assert "color: #2e7d32" in block
        assert cp.mix("#2e7d32", "#ffffff", 0.97) in block

    def test_the_fingerprint_follows_the_look(self) -> None:
        base = cp.PlayerDesign()
        assert base.fingerprint == cp.PlayerDesign().fingerprint
        assert base.fingerprint != cp.PlayerDesign(style="stripe").fingerprint
        assert base.fingerprint != cp.PlayerDesign(color="#2e7d32").fingerprint
        assert base.fingerprint != cp.PlayerDesign(label="Hør siden").fingerprint

    def test_the_file_link_is_relative_to_the_course(self) -> None:
        assert cp.player_src("4711", "99") == SRC


class TestSplicing:
    def test_the_player_goes_on_top_and_the_rest_is_untouched(self) -> None:
        block = cp.player_html(cp.PlayerDesign(), SRC)
        assert cp.with_player(CONTENT, block) == f"{block}\n{CONTENT}"

    def test_publishing_again_replaces_the_player(self) -> None:
        old = cp.player_html(cp.PlayerDesign(), SRC)
        new = cp.player_html(cp.PlayerDesign(style="mork"), "/courses/4711/files/100/download")
        body = cp.with_player(cp.with_player(CONTENT, old), new)
        assert body == f"{new}\n{CONTENT}"
        assert len(cp.find_player_blocks(body)) == 1

    def test_a_block_rewritten_by_canvas_is_still_found(self) -> None:
        # Canvas may reorder attributes, drop the class and reformat the markup.
        rewritten = (
            '<div style="margin: 18px 0px 28px;" data-canvas-audio="1">\n'
            '    <div style="display: flex;"><span>🎧</span> <span>Lytt</span></div>\n'
            '    <audio controls="controls"><source src="x" type="audio/mpeg"></audio>\n'
            "</div>"
        )
        body = f"<p>Før</p>\n{rewritten}\n{CONTENT}"
        assert cp.strip_players(body) == f"<p>Før</p>\n{CONTENT}"

    def test_the_class_alone_is_enough(self) -> None:
        body = f'<div class="foo canvas-audio-player"><div></div></div>\n{CONTENT}'
        assert cp.strip_players(body) == CONTENT

    def test_divs_around_the_player_are_left_alone(self) -> None:
        block = cp.player_html(cp.PlayerDesign(), SRC)
        before = '<div class="wrap"><p>x</p></div>\n'
        after = "<div><div>y</div></div>"
        assert cp.strip_players(f"{before}{block}\n{after}") == f"{before}{after}"

    def test_every_copy_is_removed(self) -> None:
        block = cp.player_html(cp.PlayerDesign(), SRC)
        body = f"{block}\n<p>A</p>\n{block}\n<p>B</p>"
        assert cp.strip_players(body) == "<p>A</p>\n<p>B</p>"

    def test_windows_line_endings_are_handled(self) -> None:
        block = cp.player_html(cp.PlayerDesign(), SRC)
        content = "<p>A</p>\r\n<div>\r\n<p>B</p>\r\n</div>"
        body = f"<p>Start</p>\r\n{block}\n{content}"
        assert cp.strip_players(body) == f"<p>Start</p>\r\n{content}"

    def test_a_page_without_a_player_is_returned_as_it_is(self) -> None:
        assert cp.strip_players(CONTENT) == CONTENT
        assert cp.has_player(CONTENT) is False

    def test_a_damaged_block_is_refused(self) -> None:
        with pytest.raises(ValueError, match="slutten"):
            cp.with_player('<div data-canvas-audio="1"><div>' + CONTENT, "<div></div>")


class TestNarration:
    def test_the_player_is_not_read_aloud(self) -> None:
        block = cp.player_html(cp.PlayerDesign(label="Hør på denne siden"), SRC)
        text = html_to_text(cp.with_player(CONTENT, block))
        assert "Hør på denne siden" not in text
        assert text == html_to_text(CONTENT)
