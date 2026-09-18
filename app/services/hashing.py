"""Content hashing and change detection.

This is the module that keeps the bill down. Nothing is regenerated because a
sync happened — only because the content that would be spoken actually changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.models.core import AIProvider, PageAudioStatus
from app.services.prompts import PROMPT_VERSION


def text_hash(text: str) -> str:
    """Stable hash of text content, insensitive to trailing whitespace differences."""
    normalized = "\n".join(line.rstrip() for line in (text or "").strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def audio_is_current(page: Any) -> bool:
    """True when the existing audio was made from exactly today's text and Canvas HTML."""
    audio = getattr(page, "audio", None)
    if audio is None:
        return False
    if audio.source_canvas_hash and audio.source_canvas_hash != page.original_hash:
        return False
    return audio.source_text_hash == text_hash(page.tts_text)


def ai_result_is_reusable(page: Any, provider: str, model: str) -> bool:
    """True when the stored processed text can be reused without paying again.

    A manual edit always wins: we never silently overwrite text the user typed.
    """
    if provider == AIProvider.NONE:
        return True
    if page.manually_edited:
        return True
    if not page.processed_text:
        return False
    return (
        page.processed_by == provider
        and page.processed_model == model
        and page.processed_source_hash == page.original_hash
        and page.prompt_version == PROMPT_VERSION
    )


def audio_status(
    *,
    has_audio: bool,
    audio_text_hash: str,
    audio_canvas_hash: str,
    tts_text: str,
    page_hash: str,
    last_error: str,
) -> str:
    """The status rule itself, on plain values.

    The page list needs this for hundreds of rows without loading whole pages,
    and :func:`page_audio_status` needs it for one ORM object. They must never
    disagree, so there is only one copy of the rule.
    """
    if not has_audio:
        return PageAudioStatus.FAILED if last_error else PageAudioStatus.NOT_GENERATED
    if audio_canvas_hash and audio_canvas_hash != page_hash:
        return PageAudioStatus.CANVAS_CHANGED
    if audio_text_hash != text_hash(tts_text):
        return PageAudioStatus.TEXT_CHANGED
    return PageAudioStatus.UP_TO_DATE


def page_audio_status(page: Any) -> str:
    """Derive the badge shown next to a page in the GUI."""
    audio = getattr(page, "audio", None)
    return audio_status(
        has_audio=audio is not None,
        audio_text_hash=audio.source_text_hash if audio else "",
        audio_canvas_hash=audio.source_canvas_hash if audio else "",
        tts_text=page.tts_text,
        page_hash=page.original_hash,
        last_error=page.last_error,
    )


#: Norwegian labels and icons for the derived status, used by the templates.
STATUS_LABELS: dict[str, tuple[str, str, str]] = {
    PageAudioStatus.UP_TO_DATE: ("✓", "Oppdatert", "ok"),
    PageAudioStatus.NOT_GENERATED: ("○", "Ikke generert", "muted"),
    # Covers both causes: the Canvas page changed, or the app now derives a
    # different text from the same page (an improved cleaner, a new
    # pronunciation rule). Either way the source text is not what it was.
    PageAudioStatus.CANVAS_CHANGED: ("⚠", "Teksten fra Canvas er endret", "warn"),
    PageAudioStatus.TEXT_CHANGED: ("⚠", "Teksten er endret", "warn"),
    PageAudioStatus.FAILED: ("✕", "Feilet", "error"),
}


def status_label(status: str) -> tuple[str, str, str]:
    return STATUS_LABELS.get(status, ("○", "Ukjent", "muted"))
