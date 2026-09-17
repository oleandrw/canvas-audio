"""Configuration and secret handling.

Resolution order for every key is: **process environment → `.env` file → default**.

Values that come from the real process environment are treated as read-only by the
GUI, because on a server the environment is the authoritative source and rewriting
`.env` there would silently do nothing. Values that come from `.env` may be updated
from the Settings page; that write is atomic and is intended for local or private
deployments only (documented in the README).

Secrets are never returned to the browser in full — only :func:`mask` output.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Keys that hold credentials. Never logged, never returned unmasked.
SECRET_KEYS: tuple[str, ...] = (
    "CANVAS_TOKEN",
    "ELEVENLABS_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "SOUNDCLOUD_CLIENT_SECRET",
    "APP_PASSWORD",
    "SECRET_KEY",
)

#: Non-secret configuration keys, with their defaults.
PLAIN_DEFAULTS: dict[str, str] = {
    "CANVAS_URL": "",
    "SOUNDCLOUD_CLIENT_ID": "",
    "SOUNDCLOUD_REDIRECT_URI": "http://localhost:8000/api/soundcloud/callback",
    "DATABASE_URL": "",
    "DATA_DIR": "",
    "OUTPUT_DIR": "",
    "PRICING_FILE": "",
    "LOG_LEVEL": "INFO",
    "HTTPS_ONLY": "0",
    "HTTP_TIMEOUT": "60",
}

ALL_KEYS: tuple[str, ...] = SECRET_KEYS + tuple(PLAIN_DEFAULTS)

Source = Literal["env", "file", "default"]


def mask(value: str | None) -> str:
    """Return a display-safe fingerprint of a secret.

    Never returns enough characters to be useful to an attacker, but enough for a
    human to tell two keys apart.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}…{value[-4:]}"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a `.env` file into a dict. Unknown syntax is skipped, not raised."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(slots=True)
class Settings:
    """Effective configuration, resolved once and refreshed after a settings write."""

    canvas_url: str
    canvas_token: str
    elevenlabs_api_key: str
    openai_api_key: str
    anthropic_api_key: str
    soundcloud_client_id: str
    soundcloud_client_secret: str
    soundcloud_redirect_uri: str
    app_password: str
    secret_key: str
    database_url: str
    data_dir: Path
    output_dir: Path
    pricing_file: Path
    log_level: str
    https_only: bool
    http_timeout: float

    @property
    def auth_enabled(self) -> bool:
        return bool(self.app_password)


#: Where `.env` lives unless a caller says otherwise. Tests point this somewhere
#: harmless, so a stray `reset_store()` can never reach the real file.
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigStore:
    """Owns the `.env` file and answers "where did this value come from?"."""

    def __init__(self, env_path: Path | None = None) -> None:
        self.env_path = env_path or DEFAULT_ENV_PATH
        self._file_values: dict[str, str] = {}
        self.reload()

    # -- reading ---------------------------------------------------------

    def reload(self) -> None:
        self._file_values = parse_env_file(self.env_path)

    def get(self, key: str, default: str = "") -> str:
        env_value = os.environ.get(key)
        if env_value:
            return env_value
        file_value = self._file_values.get(key)
        if file_value:
            return file_value
        return PLAIN_DEFAULTS.get(key, default)

    def source(self, key: str) -> Source:
        if os.environ.get(key):
            return "env"
        if self._file_values.get(key):
            return "file"
        return "default"

    def is_locked(self, key: str) -> bool:
        """True when the value comes from the real environment and the GUI must not edit it."""
        return self.source(key) == "env"

    # -- writing ---------------------------------------------------------

    def update(self, values: dict[str, str]) -> tuple[list[str], list[str]]:
        """Persist `values` to the `.env` file.

        Keys whose value is currently supplied by a process environment variable are
        skipped — writing them would be a silent no-op at runtime.

        Returns ``(written_keys, skipped_keys)``.
        """
        written: list[str] = []
        skipped: list[str] = []
        merged = dict(self._file_values)
        for key, value in values.items():
            if key not in ALL_KEYS:
                continue
            if self.is_locked(key):
                skipped.append(key)
                continue
            if value == "":
                merged.pop(key, None)
            else:
                merged[key] = value
            written.append(key)

        if written:
            self._atomic_write(merged)
            self.reload()
        return written, skipped

    def _atomic_write(self, values: dict[str, str]) -> None:
        lines = [
            "# Written by the Canvas → Lyd settings page.",
            "# This file must never be committed. See .env.example for documentation.",
            "",
        ]
        for key in ALL_KEYS:
            if key in values:
                lines.append(f"{key}={values[key]}")
        # Preserve any unrecognised keys the user added by hand.
        for key, value in values.items():
            if key not in ALL_KEYS:
                lines.append(f"{key}={value}")
        content = "\n".join(lines) + "\n"

        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.env_path.parent), prefix=".env.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            _restrict_permissions(tmp_path)
            os.replace(tmp_path, self.env_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        _restrict_permissions(self.env_path)

    # -- derived ---------------------------------------------------------

    def settings(self) -> Settings:
        data_dir = Path(self.get("DATA_DIR") or PROJECT_ROOT / "data").resolve()
        output_dir = Path(self.get("OUTPUT_DIR") or PROJECT_ROOT / "output").resolve()
        pricing_file = Path(self.get("PRICING_FILE") or PROJECT_ROOT / "pricing.toml").resolve()
        database_url = self.get("DATABASE_URL") or f"sqlite:///{(data_dir / 'canvas_audio.db')}"
        return Settings(
            canvas_url=self.get("CANVAS_URL").rstrip("/"),
            canvas_token=self.get("CANVAS_TOKEN"),
            elevenlabs_api_key=self.get("ELEVENLABS_API_KEY"),
            openai_api_key=self.get("OPENAI_API_KEY"),
            anthropic_api_key=self.get("ANTHROPIC_API_KEY"),
            soundcloud_client_id=self.get("SOUNDCLOUD_CLIENT_ID"),
            soundcloud_client_secret=self.get("SOUNDCLOUD_CLIENT_SECRET"),
            soundcloud_redirect_uri=self.get("SOUNDCLOUD_REDIRECT_URI"),
            app_password=self.get("APP_PASSWORD"),
            secret_key=self.get("SECRET_KEY") or _ephemeral_secret_key(),
            database_url=database_url,
            data_dir=data_dir,
            output_dir=output_dir,
            pricing_file=pricing_file,
            log_level=self.get("LOG_LEVEL").upper() or "INFO",
            https_only=self.get("HTTPS_ONLY") in {"1", "true", "True", "yes"},
            http_timeout=float(self.get("HTTP_TIMEOUT") or 60),
        )


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0600. Windows ignores POSIX modes; this is a no-op there."""
    # pragma: no cover - platform dependent
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)


_EPHEMERAL_SECRET: str | None = None


def _ephemeral_secret_key() -> str:
    """Fallback key for when SECRET_KEY is unset and could not be saved.

    Regenerated per process, so sessions and the stored SoundCloud token do not
    survive a restart. :func:`ensure_secret_key` avoids this in normal use.
    """
    global _EPHEMERAL_SECRET
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
    return _EPHEMERAL_SECRET


def ensure_secret_key(store: ConfigStore | None = None) -> str:
    """Give SECRET_KEY a value that survives restarts.

    The key signs the session cookie and encrypts the stored SoundCloud token. A key
    that changes on every start makes that token unreadable, which silently
    disconnects SoundCloud and with it automatic publishing. When neither the
    environment nor `.env` sets a key, one is generated once and written to `.env`,
    which is gitignored like every other secret there.

    Returns where the key comes from: ``"env"``, ``"file"``, ``"generated"``, or
    ``"ephemeral"`` when `.env` could not be written.
    """
    store = store or get_store()
    source = store.source("SECRET_KEY")
    if source != "default":
        return source
    try:
        store.update({"SECRET_KEY": secrets.token_urlsafe(48)})
    except OSError:
        return "ephemeral"
    return "generated"


_store: ConfigStore | None = None


def get_store() -> ConfigStore:
    global _store
    if _store is None:
        _store = ConfigStore()
    return _store


def get_settings() -> Settings:
    """Current effective settings. Cheap enough to call per request."""
    return get_store().settings()


def reset_store(env_path: Path | None = None) -> ConfigStore:
    """Replace the process-wide store. Used by tests and after a settings write."""
    global _store
    _store = ConfigStore(env_path)
    return _store
