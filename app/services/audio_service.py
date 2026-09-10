"""Where audio files live on disk, and how they get there.

Directory layout mirrors Canvas so the result is browsable without the app::

    output/
      mitt-emne/
        01-introduksjon/
          01-velkommen.mp3
          02-hva-skal-vi-laere.mp3
        02-tema-1/
          01-introduksjon.mp3

Every path is built through :func:`app.security.safe_join`, so a hostile Canvas
title cannot write outside the output directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_config import get_logger
from app.security import safe_join
from app.services.hashing import file_hash
from app.services.naming import audio_filename, course_dirname, module_dirname, unique_name

logger = get_logger("app.services.audio")

AUDIO_EXTENSION = ".mp3"
NO_MODULE_DIRNAME = "00-uten-modul"


@dataclass(slots=True)
class StoredAudio:
    relative_path: str
    absolute_path: Path
    size_bytes: int
    duration_seconds: float
    file_hash: str


def output_root() -> Path:
    root = get_settings().output_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def course_directory(course: Any) -> str:
    return course.slug or course_dirname(course.name)


def module_directory(module: Any | None) -> str:
    if module is None:
        return NO_MODULE_DIRNAME
    return module.slug or module_dirname(module.name, module.position)


def build_relative_path(page: Any, *, style: str = "slug") -> str:
    """Compute the on-disk relative path for a page, avoiding collisions."""
    course_dir = course_directory(page.course)
    module_dir = module_directory(page.module)
    filename = audio_filename(page.title, page.position, style=style, extension=AUDIO_EXTENSION)

    target_dir = safe_join(output_root(), course_dir, module_dir)
    existing = (
        {entry.name for entry in target_dir.glob(f"*{AUDIO_EXTENSION}")}
        if target_dir.is_dir()
        else set()
    )

    # A regeneration keeps its own filename; only a genuine clash gets a suffix.
    current = getattr(page.audio, "relative_path", "") if getattr(page, "audio", None) else ""
    if current and Path(current).name in existing:
        existing.discard(Path(current).name)

    stem = filename[: -len(AUDIO_EXTENSION)]
    stem = unique_name(stem, existing, suffix=AUDIO_EXTENSION)
    return f"{course_dir}/{module_dir}/{stem}{AUDIO_EXTENSION}"


def resolve_path(relative_path: str) -> Path:
    """Turn a stored relative path into an absolute one, rejecting traversal."""
    return safe_join(output_root(), *Path(relative_path.replace("\\", "/")).parts)


def mp3_duration(path: Path) -> float:
    """Best-effort duration in seconds. A missing duration must not fail a job."""
    try:
        from mutagen.mp3 import MP3

        audio = MP3(str(path))
        return float(getattr(audio.info, "length", 0.0) or 0.0)
    except Exception as exc:  # pragma: no cover - depends on encoder output
        logger.warning("Could not read MP3 duration for %s: %s", path.name, exc)
        return 0.0


def write_audio(relative_path: str, data: bytes) -> StoredAudio:
    """Write MP3 bytes to disk and return everything the database needs."""
    absolute = resolve_path(relative_path)
    absolute.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary file first so an interrupted run never leaves a
    # half-written MP3 that looks complete.
    temp_path = absolute.with_suffix(absolute.suffix + ".part")
    temp_path.write_bytes(data)
    temp_path.replace(absolute)

    stored = StoredAudio(
        relative_path=relative_path,
        absolute_path=absolute,
        size_bytes=absolute.stat().st_size,
        duration_seconds=mp3_duration(absolute),
        file_hash=file_hash(absolute),
    )
    logger.info(
        "Wrote audio file path=%s bytes=%d duration=%.1fs",
        relative_path,
        stored.size_bytes,
        stored.duration_seconds,
    )
    return stored


def delete_audio(relative_path: str) -> bool:
    try:
        absolute = resolve_path(relative_path)
    except ValueError:
        return False
    if absolute.is_file():
        absolute.unlink()
        return True
    return False


def audio_exists(relative_path: str) -> bool:
    if not relative_path:
        return False
    try:
        return resolve_path(relative_path).is_file()
    except ValueError:
        return False


def format_duration(seconds: float) -> str:
    """``93.4`` → ``"1:33"``. Used in templates."""
    total = int(round(seconds or 0))
    minutes, remainder = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remainder:02d}"
    return f"{minutes}:{remainder:02d}"


def format_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
