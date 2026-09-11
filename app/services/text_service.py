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
# A real tag: "<" immediately followed by a name. "a < b and c > d" must not match.
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")

#: Marks that already give the speech engine a pause at the end of a block.
_TERMINAL = ".!?:…"
#: Closing quotes that may follow that mark: «Hei.» Brackets are deliberately not
#: skipped: in "(Arbeidsmiljøloven, osv.)" the full stop belongs to the
#: abbreviation, so the sentence still needs its own after the bracket.
_CLOSERS = "\"'»”’"
#: A block ending in one of these reads as unfinished; it becomes a full stop.
_WEAK_ENDINGS = ",;"
#: A line broken with <br> right after one of these words is a sentence that
#: simply wrapped ("...en setning som" + "fortsetter her"), so it is joined back.
#: Any other line break ends the sentence, including a list typed one item per
#: line in lower case, which is far more common in real course pages.
_CONTINUATION_WORDS = frozenset(
    # Norwegian conjunctions, subjunctions, prepositions and articles.
    "og eller men samt som at å i på til for fra med av om ved mot hos uten "  # noqa: SIM905 - one word list reads better
    "gjennom mellom blant en et ei den det de der hvor når hvis fordi mens enn både "
    # The same for English pages.
    "and or but the a an of to in on with that which who from by as".split()
)
_LAST_WORD = re.compile(r"(\w+)\W*$")
#: Hand-typed bullets at the start of a line ("• Punkt", "- Punkt"). The marker
#: is dropped, since it carries no meaning when read aloud.
_BULLET = re.compile(r"^[•●○◦▪▫■□·‣⁃∙*\-–—]+\s+")


def end_sentence(text: str) -> str:
    """Make sure a block ends with a sentence mark, so narration pauses there.

    A trailing comma or semicolon becomes a full stop. A colon, question mark or
    exclamation mark is kept, and closing quotes are looked past.
    """
    stripped = text.rstrip()
    body = stripped.rstrip(_CLOSERS)
    if not body:
        return stripped
    if body[-1] in _TERMINAL:
        return stripped
    if body[-1] in _WEAK_ENDINGS:
        return body[:-1].rstrip() + stripped[len(body) :] + "."
    return stripped + "."


def strip_bullet(text: str) -> tuple[str, bool]:
    """Remove a hand-typed bullet. Returns the text and whether one was there."""
    stripped = _BULLET.sub("", text, count=1)
    return stripped, stripped != text


def ensure_sentence_endings(text: str) -> str:
    """Give every line of already-plain text a sentence ending.

    Used on AI output, which is read line by line just like cleaned HTML.
    Idempotent: running it twice changes nothing the second time.
    """
    lines: list[str] = []
    for line in (text or "").splitlines():
        content, _ = strip_bullet(line.strip())
        lines.append(end_sentence(content) if content else "")
    return "\n".join(lines).strip()


class _Renderer:
    """Walks the DOM and emits one string per logical block.

    Every block boundary ends a sentence, because that is where a listener
    expects a pause. ``<br>`` is the one soft boundary: a line that breaks
    mid-sentence, right after a word like "som" or "og", and carries on in
    lower case ("...som<br>fortsetter her") is joined back into one sentence.
    Every other line gets a full stop, including lower-case list items.
    """

    def __init__(self) -> None:
        self.blocks: list[str] = []
        self._buffer: list[str] = []
        # A line that ended at <br>, waiting to see whether the next one continues it.
        self._pending: str | None = None
        self._pending_bullet = False

    # -- buffer handling -------------------------------------------------

    def add(self, text: str) -> None:
        if text:
            self._buffer.append(text)

    def flush(self, *, soft: bool = False, terminate: bool = True) -> None:
        text = _WHITESPACE.sub(" ", "".join(self._buffer)).strip()
        self._buffer = []
        bullet = False
        if text:
            text, bullet = strip_bullet(text)

        if text and self._pending is not None and self._continues(text, bullet):
            text = f"{self._pending} {text}"
            self._pending = None
        self._seal(terminate=terminate)

        if not text:
            return
        if soft:
            self._pending, self._pending_bullet = text, bullet
        else:
            self.blocks.append(end_sentence(text) if terminate else text)

    def finish(self, *, terminate: bool = True) -> list[str]:
        """Flush what is left, including a line that ended at a final ``<br>``."""
        self.flush(terminate=terminate)
        return self.blocks

    def _continues(self, text: str, bullet: bool) -> bool:
        if bullet or self._pending_bullet or not text[:1].islower():
            return False
        last = _LAST_WORD.search(self._pending or "")
        return bool(last) and last.group(1).lower() in _CONTINUATION_WORDS

    def _seal(self, *, terminate: bool = True) -> None:
        if self._pending is not None:
            self.blocks.append(end_sentence(self._pending) if terminate else self._pending)
            self._pending = None

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
            self.flush(soft=True)
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
        if name in HEADING_TAGS or name in BLOCK_TAGS:
            # Text right before a block ends its own sentence, and so does the block.
            self.flush()
            self.walk(node)
            self.flush()
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
            item_text = " ".join(sub.finish()).strip()
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
                cell_text = " ".join(sub.finish(terminate=False)).strip().rstrip(".")
                if cell_text:
                    cells.append(cell_text)
            if cells:
                self.blocks.append(end_sentence(", ".join(cells)))


def html_to_text(html: str) -> str:
    """Convert Canvas page HTML into clean, readable prose.

    Every paragraph, heading, list item, table row and line ends with a
    sentence mark, so the narration pauses where a reader would. Malformed HTML
    is tolerated: ``html.parser`` is lenient and any residual markup is stripped
    by the final guard.
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

    text = "\n\n".join(block for block in renderer.finish() if block)
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
