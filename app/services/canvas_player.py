"""The audio player embedded at the top of a Canvas page.

Canvas strips ``<style>`` and ``<script>``, so every design is inline CSS on a few
plain elements (div, span, audio, source, a). The outer div carries a marker so
the app can find the block again: to swap in a new file or design, to remove it,
and to leave it out of the text that is read aloud.

Splicing works on the raw HTML string. Only the player block itself is inserted
or cut out; everything the teacher wrote stays byte for byte as Canvas stored it.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

MARKER_CLASS = "canvas-audio-player"
MARKER_ATTR = "data-canvas-audio"
MARKER_VERSION = "1"

DEFAULT_STYLE = "kort"
DEFAULT_COLOR = "#0b5bd3"
DEFAULT_LABEL = "Lytt til denne siden"
DEFAULT_ICON = "🎧"
MAX_LABEL_LENGTH = 80

#: style key -> Norwegian label shown in the GUI
STYLES: dict[str, str] = {
    "kort": "Kort",
    "stripe": "Stripe",
    "kompakt": "Kompakt",
    "minimal": "Minimal",
    "mork": "Mørk",
}

#: colour key -> (label, accent)
COLORS: dict[str, tuple[str, str]] = {
    "blaa": ("Blå", "#0b5bd3"),
    "teal": ("Petrol", "#1f7a7a"),
    "gronn": ("Grønn", "#2e7d32"),
    "lilla": ("Lilla", "#6d3fd4"),
    "oransje": ("Oransje", "#c05621"),
    "roed": ("Rød", "#c53030"),
    "graa": ("Grå", "#4a5568"),
}

#: icon -> label; "" means no icon
ICONS: dict[str, str] = {
    "🎧": "Hodetelefoner",
    "🔊": "Høyttaler",
    "🎙️": "Mikrofon",
    "▶️": "Avspilling",
    "": "Uten ikon",
}

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

#: The exact tints of the original design, kept for the default blue.
_DEFAULT_TINTS = {"background": "#f7faff", "border": "#dfe9f5", "bubble": "#eaf2fd"}


def is_hex_color(value: str) -> bool:
    return bool(_HEX_COLOR.match(value or ""))


def mix(color: str, other: str, weight: float) -> str:
    """Blend ``color`` towards ``other``; ``weight`` is the share of ``other``."""
    a = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(
        f"{round(x * (1 - weight) + y * weight):02x}" for x, y in zip(a, b, strict=True)
    )


@dataclass(frozen=True, slots=True)
class PlayerDesign:
    style: str = DEFAULT_STYLE
    color: str = DEFAULT_COLOR
    label: str = DEFAULT_LABEL
    icon: str = DEFAULT_ICON

    def normalized(self) -> PlayerDesign:
        label = " ".join((self.label or "").split())[:MAX_LABEL_LENGTH] or DEFAULT_LABEL
        return PlayerDesign(
            style=self.style if self.style in STYLES else DEFAULT_STYLE,
            color=self.color.lower() if is_hex_color(self.color) else DEFAULT_COLOR,
            label=label,
            icon=self.icon if self.icon in ICONS else DEFAULT_ICON,
        )

    @property
    def fingerprint(self) -> str:
        """Changes whenever the rendered player would look different."""
        d = self.normalized()
        raw = "|".join((MARKER_VERSION, d.style, d.color, d.label, d.icon))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _tints(color: str) -> dict[str, str]:
    if color == DEFAULT_COLOR:
        return dict(_DEFAULT_TINTS)
    return {
        "background": mix(color, "#ffffff", 0.97),
        "border": mix(color, "#ffffff", 0.86),
        "bubble": mix(color, "#ffffff", 0.91),
    }


def _css(**props: str) -> str:
    return "; ".join(f"{key.replace('_', '-')}: {value}" for key, value in props.items()) + ";"


def player_html(design: PlayerDesign, src: str) -> str:
    """The HTML block for one page. Every user-supplied value is escaped."""
    d = design.normalized()
    tints = _tints(d.color)
    label = html.escape(d.label)
    src_attr = html.escape(src, quote=True)
    icon = html.escape(d.icon)

    audio = (
        f'<audio style="{{audio_css}}" controls="controls" preload="metadata" '
        f'aria-label="{label}">'
        f'<source src="{src_attr}" type="audio/mpeg" />'
        f'<a href="{src_attr}">Last ned lydfilen</a>'
        "</audio>"
    )
    marker = f'class="{MARKER_CLASS}" {MARKER_ATTR}="{MARKER_VERSION}"'

    if d.style == "kompakt":
        outer = _css(
            display="flex",
            align_items="center",
            flex_wrap="wrap",
            gap="12px",
            margin="16px 0px 24px",
            padding="8px 14px",
            background=tints["background"],
            border=f"1px solid {tints['border']}",
            border_radius="999px",
        )
        head = _css(
            display="inline-flex",
            align_items="center",
            gap="6px",
            font_size="14px",
            color=d.color,
            white_space="nowrap",
        )
        audio_css = _css(flex="1 1 240px", min_width="200px", height="36px")
        icon_html = f"<span>{icon}</span> " if icon else ""
        header = f'<span style="{head}">{icon_html}<span>{label}</span></span>'
    else:
        if d.style == "stripe":
            outer = _css(
                margin="18px 0px 28px",
                padding="12px 16px 14px",
                background=tints["background"],
                border_left=f"4px solid {d.color}",
                border_radius="6px",
            )
            text_color, bubble = d.color, ""
        elif d.style == "minimal":
            outer = _css(margin="16px 0px 24px")
            text_color, bubble = d.color, ""
        elif d.style == "mork":
            outer = _css(
                margin="18px 0px 28px",
                padding="14px 16px 16px",
                background=mix(d.color, "#000000", 0.72),
                border=f"1px solid {mix(d.color, '#000000', 0.5)}",
                border_radius="14px",
            )
            text_color, bubble = "#ffffff", mix(d.color, "#000000", 0.45)
        else:  # kort
            outer = _css(
                margin="18px 0px 28px",
                padding="14px 16px 16px",
                background=tints["background"],
                border_radius="14px",
                border=f"1px solid {tints['border']}",
            )
            text_color, bubble = d.color, tints["bubble"]

        head = _css(
            display="flex",
            align_items="center",
            gap="8px",
            margin_bottom="10px",
            font_size="15px",
            color=text_color,
        )
        audio_css = _css(display="block", width="100%", height="40px")
        if icon and bubble:
            bubble_css = _css(
                display="inline-flex",
                align_items="center",
                justify_content="center",
                width="26px",
                height="26px",
                border_radius="50%",
                background=bubble,
                font_size="14px",
            )
            icon_html = f'<span style="{bubble_css}">{icon}</span> '
        elif icon:
            icon_html = f"<span>{icon}</span> "
        else:
            icon_html = ""
        header = f'<div style="{head}">{icon_html}<span>{label}</span></div>'

    return f'<div {marker} style="{outer}">{header}{audio.replace("{audio_css}", audio_css)}</div>'


def player_src(canvas_course_id: str, file_id: str) -> str:
    """Relative Canvas link to a course file, so it works for anyone enrolled."""
    return f"/courses/{canvas_course_id}/files/{file_id}/download"


# --------------------------------------------------------------------------- #
# Finding and splicing the block
# --------------------------------------------------------------------------- #


def _is_marker(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name == MARKER_ATTR:
            return True
        if name == "class" and MARKER_CLASS in (value or "").split():
            return True
        if name == "id" and value == MARKER_CLASS:
            return True
    return False


class _BlockFinder(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self._line_starts = [0] + [i + 1 for i, ch in enumerate(source) if ch == "\n"]
        self.blocks: list[tuple[int, int]] = []
        self._start: int | None = None
        self._depth = 0

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "div":
            return
        if self._start is not None:
            self._depth += 1
        elif _is_marker(attrs):
            self._start = self._offset()
            self._depth = 1

    def handle_endtag(self, tag: str) -> None:
        if tag != "div" or self._start is None:
            return
        self._depth -= 1
        if self._depth == 0:
            end = self.source.index(">", self._offset()) + 1
            self.blocks.append((self._start, end))
            self._start = None

    @property
    def unclosed(self) -> bool:
        return self._start is not None


def find_player_blocks(body: str) -> list[tuple[int, int]]:
    """``(start, end)`` offsets of every player block, outermost only.

    Raises ``ValueError`` for a block that starts but never ends, so a damaged
    page is left alone instead of being cut in the wrong place.
    """
    if not body or (MARKER_CLASS not in body and MARKER_ATTR not in body):
        return []
    finder = _BlockFinder(body)
    finder.feed(body)
    finder.close()
    if finder.unclosed:
        raise ValueError("Fant avspilleren på siden, men ikke slutten av den.")
    return finder.blocks


def has_player(body: str) -> bool:
    return bool(find_player_blocks(body))


def strip_players(body: str) -> str:
    """The body without any player block. Everything else is left untouched."""
    blocks = find_player_blocks(body)
    if not blocks:
        return body
    parts: list[str] = []
    cursor = 0
    for start, end in blocks:
        parts.append(body[cursor:start])
        cursor = end
        if body.startswith("\n", cursor):  # the line break inserted with the block
            cursor += 1
    parts.append(body[cursor:])
    return "".join(parts)


def with_player(body: str, block: str) -> str:
    """The body with exactly one player, at the top."""
    rest = strip_players(body or "")
    return f"{block}\n{rest}" if rest else block
