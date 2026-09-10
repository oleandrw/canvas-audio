"""Liveness endpoint. Intentionally unauthenticated so a container probe works."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.jobs.queue import get_queue

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict[str, object]:
    queue = get_queue()
    return {
        "status": "ok",
        "version": __version__,
        "worker_running": queue.current_job_id is not None,
        "queue_depth": queue.depth,
    }
