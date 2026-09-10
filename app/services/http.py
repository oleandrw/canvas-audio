"""Shared async HTTP plumbing: client construction, retry/backoff, error mapping.

Retries are deliberately narrow. Timeouts, connection errors, 429 and 5xx are
transient and get an exponential backoff (honouring ``Retry-After`` when the
server sends one). Everything else — 400, 401, 403, 404, 422 — is permanent and
fails immediately, because retrying a bad token just wastes the user's time.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from app.config import get_settings
from app.logging_config import get_logger
from app.services.errors import (
    AuthenticationError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    ServiceError,
    TransientError,
    UpstreamError,
)

logger = get_logger("app.services.http")

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.0
MAX_DELAY = 30.0


def build_client(
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create an ``AsyncClient``.

    ``transport`` exists purely so tests can inject ``httpx.MockTransport`` and
    never touch the network.
    """
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers or {},
        timeout=httpx.Timeout(timeout or settings.http_timeout, connect=15.0),
        transport=transport,
        follow_redirects=True,
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def error_snippet(response: httpx.Response, limit: int = 300) -> str:
    """A short, safe excerpt of an error body for the user-visible message."""
    try:
        payload: Any = response.json()
    except Exception:
        text = response.text or ""
        return text.strip()[:limit]
    for key in ("message", "error", "detail", "errors", "status"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, str) and value:
            return value[:limit]
        if isinstance(value, dict):
            nested = value.get("message") or value.get("detail")
            if isinstance(nested, str) and nested:
                return nested[:limit]
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str):
                return first[:limit]
            if isinstance(first, dict):
                nested = first.get("message") or first.get("detail")
                if isinstance(nested, str):
                    return nested[:limit]
    return str(payload)[:limit]


def raise_for_status(
    response: httpx.Response,
    *,
    provider: str,
    messages: dict[int, str] | None = None,
) -> None:
    """Translate a non-2xx response into the right :mod:`app.services.errors` type."""
    if response.is_success:
        return

    code = response.status_code
    custom = (messages or {}).get(code)
    detail = custom or error_snippet(response) or response.reason_phrase

    if code in (401, 403):
        raise AuthenticationError(detail, provider=provider, status_code=code)
    if code == 404:
        raise NotFoundError(detail, provider=provider, status_code=code)
    if code in (402, 413):
        raise QuotaExceededError(detail, provider=provider, status_code=code)
    if code == 429:
        raise RateLimitError(
            detail, provider=provider, status_code=code, retry_after=_retry_after_seconds(response)
        )
    if code >= 500:
        raise TransientError(detail, provider=provider, status_code=code)
    raise UpstreamError(detail, provider=provider, status_code=code)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    provider: str,
    description: str,
    max_attempts: int | None = None,
    base_delay: float | None = None,
) -> T:
    """Run ``operation`` with exponential backoff on transient failures only."""
    # Read the module constants at call time so tests can shrink the delays.
    max_attempts = max_attempts if max_attempts is not None else DEFAULT_MAX_ATTEMPTS
    base_delay = base_delay if base_delay is not None else DEFAULT_BASE_DELAY
    last_error: ServiceError | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            last_error = TransientError(
                f"Nettverksfeil mot {provider}: {type(exc).__name__}", provider=provider
            )
        except httpx.HTTPError as exc:
            last_error = TransientError(
                f"HTTP-feil mot {provider}: {type(exc).__name__}", provider=provider
            )
        except ServiceError as exc:
            if not exc.retryable:
                raise
            last_error = exc

        if attempt >= max_attempts:
            break

        delay = min(base_delay * (2 ** (attempt - 1)), MAX_DELAY)
        if isinstance(last_error, RateLimitError) and last_error.retry_after:
            delay = min(max(delay, last_error.retry_after), MAX_DELAY)
        delay += random.uniform(0, 0.4)  # noqa: S311 - jitter, not cryptography
        logger.warning(
            "%s failed (attempt %d/%d): %s — retrying in %.1fs",
            description,
            attempt,
            max_attempts,
            last_error,
            delay,
        )
        await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error
