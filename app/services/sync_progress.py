"""How far the running Canvas synchronisation has come.

Synchronising a course of a few hundred pages takes minutes, and a button that
only spins tells the user nothing — not whether it is working, and not whether it
is nearly done. The synchronisation reports into this module, and the GUI asks it
once a second.

There is deliberately no queue and no cancellation here: one synchronisation runs
at a time, and this is only what it is currently doing.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class Progress:
    running: bool = False
    course_name: str = ""
    phase: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    started_at: datetime | None = None

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(int(self.done / self.total * 100), 100)


#: Written from the event loop and read from request handlers; a lock keeps a
#: reader from seeing half an update.
_lock = threading.Lock()
_current = Progress()


def start(course_name: str = "") -> None:
    global _current
    with _lock:
        _current = Progress(
            running=True,
            course_name=course_name,
            phase="Henter struktur fra Canvas",
            started_at=datetime.now(UTC),
        )


def update(
    *, phase: str | None = None, done: int | None = None, total: int | None = None, name: str = ""
) -> None:
    with _lock:
        if not _current.running:
            return
        if phase is not None:
            _current.phase = phase
        if done is not None:
            _current.done = done
        if total is not None:
            _current.total = total
        if name:
            _current.course_name = name


def finish(message: str = "") -> None:
    with _lock:
        _current.running = False
        _current.phase = ""
        _current.message = message


def snapshot() -> dict[str, Any]:
    with _lock:
        data = asdict(_current)
    data["percent"] = _current.percent
    data["started_at"] = _current.started_at.isoformat() if _current.started_at else None
    return data


def reset() -> None:
    """Forget the current progress. Used by tests."""
    global _current
    with _lock:
        _current = Progress()
