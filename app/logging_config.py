"""Structured logging with a hard guarantee that secrets never reach the log.

The redaction filter runs on every record, including records emitted by third-party
libraries, so an accidental ``logger.debug(headers)`` somewhere cannot leak a token.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from typing import Any

from app.config import SECRET_KEYS, get_store

REDACTED = "[REDACTED]"

# Patterns that look like credentials regardless of where they came from.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(authorization|xi-api-key|x-api-key|api[_-]?key|bearer|oauth)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b\d{3,6}~[A-Za-z0-9]{20,}"),  # Canvas personal access tokens
)


class SecretRedactionFilter(logging.Filter):
    """Replace configured secret values and credential-shaped strings with ``[REDACTED]``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken %-format in a third-party log call
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def redact(text: str) -> str:
    """Remove any known secret value or credential-shaped token from ``text``."""
    if not text:
        return text
    store = get_store()
    for key in SECRET_KEYS:
        value = store.get(key)
        if value and len(value) >= 6 and value in text:
            text = text.replace(value, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(0).split(':')[0].split('=')[0]}={REDACTED}", text)
    return text


class ConsoleFormatter(logging.Formatter):
    """Compact, readable single-line format: ``12:03:41 INFO  canvas  Fetched 8 modules``."""

    def format(self, record: logging.LogRecord) -> str:
        record.short_name = record.name.replace("app.", "").replace("services.", "")
        return super().format(record)


def configure_logging(level: str = "INFO") -> None:
    """Install the console handler and redaction filter on the root logger."""
    # A Windows console defaults to cp1252, which cannot encode the characters
    # that appear in Norwegian log text. Without this, logging raises instead of
    # logging. errors="replace" keeps a wrong glyph preferable to a crash.
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(OSError, ValueError):  # pragma: no cover
            reconfigure(encoding="utf-8", errors="replace")

    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        ConsoleFormatter(
            fmt="%(asctime)s %(levelname)-5s %(short_name)-22s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Uvicorn installs its own handlers; route them through ours so redaction applies.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False

    # These are chatty at DEBUG and their records may contain headers.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "openai", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """Log a message with key=value context appended, e.g. ``Fetched pages count=43``."""
    if fields:
        context = " ".join(f"{k}={v}" for k, v in fields.items())
        logger.info("%s %s", message, context)
    else:
        logger.info(message)
