"""Path safety, token encryption and the optional single-password app lock."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path, PurePosixPath, PureWindowsPath

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status

from app.config import get_settings

SESSION_AUTH_KEY = "authenticated"


class UnsafePathError(ValueError):
    """Raised when a caller tries to escape the directory it is confined to."""


def safe_join(base: Path, *parts: str) -> Path:
    """Join ``parts`` onto ``base`` and refuse anything that escapes ``base``.

    Every filesystem write and every file read triggered by a request goes through
    this, so a crafted Canvas title or a tampered path parameter cannot reach
    outside the output directory.
    """
    base_resolved = base.resolve()
    candidate = base_resolved
    for part in parts:
        if part in ("", "."):
            continue
        pure = PurePosixPath(part.replace("\\", "/"))
        # Check the Windows reading too, so a drive letter ("C:\x", "D:rel") or a UNC
        # path is refused on every OS. On Linux "C:/x" would otherwise pass as a
        # plain folder name, while on Windows it silently replaces ``base``.
        windows = PureWindowsPath(part)
        if pure.is_absolute() or windows.drive or windows.root or ".." in pure.parts:
            raise UnsafePathError(f"Unsafe path segment: {part!r}")
        candidate = candidate / pure
    resolved = candidate.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise UnsafePathError(f"Path escapes base directory: {resolved}")
    return resolved


# --------------------------------------------------------------------------- #
# Token encryption
# --------------------------------------------------------------------------- #


class TokenCipher:
    """Fernet wrapper keyed off ``SECRET_KEY``.

    OAuth tokens rotate too often to live in `.env`, so they go in the database —
    but never in plaintext. If ``SECRET_KEY`` changes, stored tokens become
    unreadable and the user simply reconnects; :meth:`decrypt` returns ``""``
    rather than raising, so a rotated key degrades to "not connected".
    """

    def __init__(self, secret_key: str) -> None:
        digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""


def get_cipher() -> TokenCipher:
    return TokenCipher(get_settings().secret_key)


# --------------------------------------------------------------------------- #
# App lock
# --------------------------------------------------------------------------- #


def verify_password(candidate: str) -> bool:
    expected = get_settings().app_password
    if not expected:
        return True
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    return bool(request.session.get(SESSION_AUTH_KEY))


def require_auth(request: Request) -> None:
    """FastAPI dependency for API routes. UI routes redirect instead (see routes.ui)."""
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ikke innlogget")


def new_state_token() -> str:
    return secrets.token_urlsafe(32)


def pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for OAuth 2.1 PKCE (S256)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge
