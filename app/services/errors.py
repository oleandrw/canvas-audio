"""Service-level exceptions.

Every external integration raises one of these instead of leaking an ``httpx`` or
SDK exception, so routes and the job pipeline can present a message the user can
act on.

Two orthogonal facts are carried: *what kind* of failure it was (the class) and
*which provider* produced it (the ``provider`` attribute). ``retryable``
distinguishes transient failures worth a backoff from permanent ones — a bad
token will still be bad in 30 seconds.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for all integration failures."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "service",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigurationError(ServiceError):
    """A required credential or setting is missing."""


class AuthenticationError(ServiceError):
    """401/403 — the credential is wrong or lacks the needed scope."""


class NotFoundError(ServiceError):
    """404 — the requested resource does not exist."""


class RateLimitError(ServiceError):
    """429 — back off and try again."""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        provider: str = "service",
        status_code: int | None = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code)
        self.retry_after = retry_after


class QuotaExceededError(ServiceError):
    """The account is out of credits/characters. Retrying will not help."""


class TransientError(ServiceError):
    """Timeout, connection reset or 5xx."""

    retryable = True


class UpstreamError(ServiceError):
    """A well-formed request the provider rejected for a permanent reason."""


class InvalidContentError(ServiceError):
    """The content itself is unusable (empty page, unparseable HTML)."""


class SoundCloudNotConnected(ConfigurationError):
    """No usable OAuth token. The rest of the app must keep working."""

    def __init__(self, message: str = "SoundCloud er ikke tilkoblet.") -> None:
        super().__init__(message, provider="soundcloud")
