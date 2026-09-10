"""Turning Canvas titles into safe file and directory names.

Canvas titles are free text written by humans: they contain slashes, colons,
emoji, Norwegian characters, trailing dots and occasionally 300 characters of
prose. Everything that reaches the filesystem goes through this module.

The original title is always preserved in the database — normalisation here only
affects what lands on disk.
"""

from __future__ import annotations

import re
import unicodedata

MAX_FILENAME_LENGTH = 120
MAX_SLUG_LENGTH = 80

#: Characters Windows forbids outright, plus the path separators.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_MULTI_DASH = re.compile(r"-{2,}")
_NON_SLUG = re.compile(r"[^a-z0-9]+")

#: Reserved device names on Windows — a file called ``CON.mp3`` cannot be created.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_TRANSLITERATE = str.maketrans(
    {
        "æ": "ae",
        "ø": "oe",
        "å": "aa",
        "Æ": "AE",
        "Ø": "OE",
        "Å": "AA",
        "ß": "ss",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "TH",
        "đ": "d",
        "ł": "l",
        "Ł": "L",
    }
)


def _to_ascii(text: str) -> str:
    """Transliterate to ASCII, keeping Norwegian letters readable."""
    translated = text.translate(_TRANSLITERATE)
    decomposed = unicodedata.normalize("NFKD", translated)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def slugify(text: str, *, max_length: int = MAX_SLUG_LENGTH, fallback: str = "uten-navn") -> str:
    """Lowercase ASCII slug suitable for a directory or file name."""
    ascii_text = _to_ascii(text or "").lower()
    slug = _NON_SLUG.sub("-", ascii_text).strip("-")
    slug = _MULTI_DASH.sub("-", slug)
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    if not slug or slug in _RESERVED_NAMES:
        return fallback
    return slug


def sanitize_filename(
    name: str, *, max_length: int = MAX_FILENAME_LENGTH, fallback: str = "side"
) -> str:
    """Make ``name`` safe as a single filename component, keeping it readable.

    Unlike :func:`slugify` this preserves spaces and capitalisation, which is what
    ``FILENAME_STYLE=title`` wants. It still removes every character that could
    escape the directory or break on Windows.
    """
    cleaned = _ILLEGAL_CHARS.sub("", (name or "").replace("/", "-").replace("\\", "-"))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = cleaned.strip(". ")

    # Defensive: no path traversal can survive, even if the input was crafted.
    cleaned = cleaned.replace("..", "")

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].strip(". ")

    if not cleaned or cleaned.split(".")[0].lower() in _RESERVED_NAMES:
        return fallback
    return cleaned


def numbered_prefix(position: int, width: int = 2) -> str:
    """``1`` → ``"01"``. Keeps directory listings in Canvas order."""
    return str(max(position, 0)).zfill(width)


def unique_name(candidate: str, taken: set[str], *, suffix: str = "") -> str:
    """Return ``candidate`` (or ``candidate-2``, ``-3`` …) so it does not collide.

    ``taken`` is compared case-insensitively because Windows filesystems are.
    """
    lowered = {t.lower() for t in taken}
    base = candidate
    attempt = candidate
    counter = 2
    while f"{attempt}{suffix}".lower() in lowered:
        attempt = f"{base}-{counter}"
        counter += 1
    return attempt


def audio_filename(
    title: str,
    position: int,
    *,
    style: str = "slug",
    extension: str = ".mp3",
) -> str:
    """Build the filename for one page's audio.

    ``style="slug"``  → ``01-introduksjon-til-ki.mp3`` (default; ASCII only)
    ``style="title"`` → ``01 - Introduksjon til KI.mp3`` (keeps the original title)
    """
    prefix = numbered_prefix(position)
    if style == "title":
        stem = sanitize_filename(title, max_length=MAX_FILENAME_LENGTH - len(prefix) - 3)
        return f"{prefix} - {stem}{extension}"
    stem = slugify(title, max_length=MAX_SLUG_LENGTH)
    return f"{prefix}-{stem}{extension}"


def module_dirname(name: str, position: int) -> str:
    """``Modul 1 – Introduksjon`` at position 1 → ``01-modul-1-introduksjon``."""
    return f"{numbered_prefix(position)}-{slugify(name, fallback='modul')}"


def course_dirname(name: str) -> str:
    return slugify(name, fallback="emne")
