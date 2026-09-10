"""A single in-process job queue.

This is deliberately not Celery/Redis. One asyncio task drains an
``asyncio.Queue`` of job ids; all state that matters lives in SQLite, so
progress survives a restart and the GUI can simply poll.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import run_db, session_scope
from app.jobs.pipeline import run_job
from app.logging_config import get_logger
from app.models.core import GenerationJob, ItemStatus, JobStatus

logger = get_logger("app.jobs.queue")


def recover_interrupted_jobs(session: Session) -> list[int]:
    """Put jobs that were running when the process died back in the queue."""
    stale = (
        session.execute(
            select(GenerationJob).where(
                GenerationJob.status.in_([JobStatus.RUNNING, JobStatus.QUEUED])
            )
        )
        .scalars()
        .all()
    )
    recovered: list[int] = []
    for job in stale:
        for item in job.items:
            if item.status not in ItemStatus.TERMINAL:
                item.status = ItemStatus.PENDING
                item.message = ""
        job.completed_items = sum(1 for i in job.items if i.status == ItemStatus.DONE)
        job.failed_items = sum(1 for i in job.items if i.status == ItemStatus.FAILED)
        job.skipped_items = sum(1 for i in job.items if i.status == ItemStatus.SKIPPED)
        job.status = JobStatus.QUEUED
        job.started_at = None
        recovered.append(job.id)
    return recovered


class JobQueue:
    """Owns the worker task and the set of cancellation requests."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._cancelled: set[int] = set()
        self._current_job: int | None = None
        self._tts_client_factory: Any | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._worker is not None:
            return
        with session_scope() as session:
            recovered = recover_interrupted_jobs(session)
        for job_id in recovered:
            self._queue.put_nowait(job_id)
        if recovered:
            logger.info("Recovered %d interrupted job(s): %s", len(recovered), recovered)
        self._worker = asyncio.create_task(self._run(), name="generation-worker")
        logger.info("Job worker started")

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker
        self._worker = None
        logger.info("Job worker stopped")

    # -- queue -----------------------------------------------------------

    async def enqueue(self, job_id: int) -> None:
        self._cancelled.discard(job_id)
        await self._queue.put(job_id)
        logger.info("Job queued id=%s depth=%d", job_id, self._queue.qsize())

    def enqueue_nowait(self, job_id: int) -> None:
        self._cancelled.discard(job_id)
        self._queue.put_nowait(job_id)

    def cancel(self, job_id: int) -> None:
        self._cancelled.add(job_id)
        logger.info("Cancellation requested for job id=%s", job_id)

    def is_cancelled(self, job_id: int) -> bool:
        return job_id in self._cancelled

    @property
    def current_job_id(self) -> int | None:
        return self._current_job

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    # -- worker ----------------------------------------------------------

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            self._current_job = job_id
            try:
                if self.is_cancelled(job_id):
                    await run_db(lambda session, jid=job_id: _mark_cancelled(session, jid))
                    continue
                await run_job(job_id, is_cancelled=lambda jid=job_id: self.is_cancelled(jid))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Job %s crashed", job_id)
            finally:
                self._cancelled.discard(job_id)
                self._current_job = None
                self._queue.task_done()


def _mark_cancelled(session: Session, job_id: int) -> None:
    job = session.get(GenerationJob, job_id)
    if job is None:
        return
    job.status = JobStatus.CANCELLED
    for item in job.items:
        if item.status not in ItemStatus.TERMINAL:
            item.status = ItemStatus.SKIPPED
            item.message = "Avbrutt før start."


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue


def reset_queue() -> None:
    """Used by tests so each case gets a clean queue."""
    global _queue
    _queue = None
