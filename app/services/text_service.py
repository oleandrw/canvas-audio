"""Canvas HTML → text a speech engine can read out loud.

Canvas page bodies are editor-generated HTML full of things that make no sense
when spoken: navigation, styling wrappers, raw URLs, empty anchors, layout
tables. This module reduces a page to plain prose while keeping every piece of
actual content.

Nothing else in the application is allowed to send HTML to a text-to-speech or
LLM provider — :func:`ensure_plain_text` is the guard that enforces it.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.services.errors import InvalidContentError

#: Removed with their contents — never spoken, never useful.
DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "object",
    "embed",
    "audio",
    "video",
    "img",
    "map",
    "area",
    "link",
    "meta",
)

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "main",
    "aside",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
    "address",
    "dl",
    "dt",
    "dd",
    "ul",
    "ol",
    "hr",
} | HEADING_TAGS

_WHITESPACE = re.compile(r"\s+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_URL_LIKE = re.compile(r"^(https?://|www\.|mailto:)\S*$", re.IGNORECASE)
_SENTENCE_END = ".!?:;,"
# A real tag: "<" immediately followed by a name. "a < b and c > d" must not match.
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")


class _Renderer:
    """Walks the DOM and emits one string per logical block."""

    def __init__(self) -> None:
        self.blocks: list[str] = []
        self._buffer: list[str] = []

    # -- buffer handling -------------------------------------------------

    def add(self, text: str) -> None:
        if text:
            self._buffer.append(text)

    def flush(self, *, terminate: bool = False) -> None:
        text = _WHITESPACE.sub(" ", "".join(self._buffer)).strip()
        self._buffer = []
        if not text:
            return
        if terminate and text[-1] not in _SENTENCE_END:
            text += "."
        self.blocks.append(text)

    # -- traversal -------------------------------------------------------

    def walk(self, node: Tag) -> None:
        for child in list(node.children):
            self._visit(child)

    def _visit(self, node: object) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            self.add(str(node))
            return
        if not isinstance(node, Tag):
            return

        name = node.name.lower()

        if name in DROP_TAGS:
            return
        if name == "br":
            self.flush()
            return
        if name == "hr":
            self.flush()
            return
        if name == "a":
            self._visit_anchor(node)
            return
        if name in ("ul", "ol"):
            self._visit_list(node, ordered=name == "ol")
            return
        if name == "table":
            self._visit_table(node)
            return
        if name in HEADING_TAGS:
            self.flush()
            self.walk(node)
            self.flush(terminate=True)
            return
        if name in BLOCK_TAGS:
            self.flush()
            self.walk(node)
            self.flush(terminate=True)
            return

        # Inline element (span, strong, em, code, ...)
        self.walk(node)

    def _visit_anchor(self, node: Tag) -> None:
        text = _WHITESPACE.sub(" ", node.get_text(" ", strip=True)).strip()
        if not text:
            return
        if _URL_LIKE.match(text):
            # A bare URL read aloud is noise; the surrounding sentence keeps the meaning.
            return
        self.add(f" {text} ")

    def _visit_list(self, node: Tag, *, ordered: bool) -> None:
        self.flush()
        index = 1
        for item in node.find_all("li", recursive=False):
            sub = _Renderer()
            sub.walk(item)
            sub.flush(terminate=True)
            item_text = " ".join(sub.blocks).strip()
            if not item_text:
                continue
            if ordered:
                item_text = f"{index}. {item_text}"
                index += 1
            self.blocks.append(item_text)

    def _visit_table(self, node: Tag) -> None:
        self.flush()
        for row in node.find_all("tr"):
            cells: list[str] = []
            for cell in row.find_all(["th", "td"], recursive=False):
                sub = _Renderer()
                sub.walk(cell)
                sub.flush()
                cell_text = " ".join(sub.blocks).strip().rstrip(".")
                if cell_text:
                    cells.append(cell_text)
            if cells:
                line = ", ".join(cells)
                if line[-1] not in _SENTENCE_END:
                    line += "."
                self.blocks.append(line)


def html_to_text(html: str) -> str:
    """Convert Canvas page HTML into clean, readable prose.

    Malformed HTML is tolerated — ``html.parser`` is lenient and any residual
    markup is stripped by the final guard.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag_name in DROP_TAGS:
        for element in soup.find_all(tag_name):
            element.decompose()

    renderer = _Renderer()
    renderer.walk(soup)
    renderer.flush(terminate=True)

    text = "\n\n".join(block for block in renderer.blocks if block)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def contains_html(text: str) -> bool:
    return bool(_HTML_TAG.search(text or ""))


def ensure_plain_text(text: str, *, context: str = "tekst") -> str:
    """Guard used immediately before any paid provider call.

    Raises rather than silently sending markup to a provider that would read
    ``less than p greater than`` out loud (and bill for the characters).
    """
    if not text or not text.strip():
        raise InvalidContentError(f"{context.capitalize()} er tom — ingenting å lese opp.")
    if contains_html(text):
        raise InvalidContentError(
            f"{context.capitalize()} inneholder fortsatt HTML. Kjør tekstrensing på nytt."
        )
    return text.strip()


def word_count(text: str) -> int:
    return len(text.split())


def summarize_structure(html: str) -> dict[str, int]:
    """Small diagnostic used in the page detail view."""
    soup = BeautifulSoup(html or "", "html.parser")
    return {
        "headings": len(soup.find_all(list(HEADING_TAGS))),
        "paragraphs": len(soup.find_all("p")),
        "lists": len(soup.find_all(["ul", "ol"])),
        "tables": len(soup.find_all("table")),
        "links": len(soup.find_all("a")),
        "images": len(soup.find_all("img")),
    }
