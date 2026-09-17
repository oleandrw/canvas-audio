"""Settings resolution, preferences and the audio-file helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.security import UnsafePathError
from app.services.app_settings import (
    DEFAULTS,
    get_bool,
    get_float,
    load_preferences,
    set_many,
    set_value,
)
from app.services.audio_service import (
    audio_exists,
    build_relative_path,
    format_bytes,
    format_duration,
    resolve_path,
    write_audio,
)
from tests.conftest import make_course, make_module, make_page


class TestSettings:
    def test_paths_come_from_the_environment(self, env: Path) -> None:
        settings = get_settings()
        assert settings.data_dir == (env / "data").resolve()
        assert settings.output_dir == (env / "output").resolve()
        assert settings.canvas_url == "https://canvas.example.test"

    def test_trailing_slash_is_stripped_from_the_canvas_url(self, env: Path, monkeypatch) -> None:
        monkeypatch.setenv("CANVAS_URL", "https://canvas.example.test/")
        from app.config import reset_store

        reset_store(env_path=env / ".env")
        assert get_settings().canvas_url == "https://canvas.example.test"

    def test_auth_is_disabled_without_a_password(self, env: Path) -> None:
        assert get_settings().auth_enabled is False

    def test_a_secret_key_is_always_available(self, env: Path, monkeypatch) -> None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
        from app.config import reset_store

        reset_store(env_path=env / ".env")
        assert len(get_settings().secret_key) > 20

    def test_saving_a_key_keeps_writing_to_the_same_file(self, env: Path, monkeypatch) -> None:
        """A save must not move the store back to the default .env behind our back."""
        from fastapi.testclient import TestClient

        from app.config import get_store, reset_store
        from app.main import create_app

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        reset_store(env_path=env / ".env")
        with TestClient(create_app()) as client:
            client.post("/api/settings", json={"openai_api_key": "sk-testverdi"})

        assert get_store().env_path == env / ".env"
        assert "sk-testverdi" in (env / ".env").read_text(encoding="utf-8")

    def test_a_missing_secret_key_is_generated_once_and_saved(self, env: Path, monkeypatch) -> None:
        from app.config import ensure_secret_key, parse_env_file, reset_store

        monkeypatch.delenv("SECRET_KEY", raising=False)
        assert ensure_secret_key(reset_store(env_path=env / ".env")) == "generated"
        key = get_settings().secret_key
        assert parse_env_file(env / ".env")["SECRET_KEY"] == key

        restarted = reset_store(env_path=env / ".env")  # a new process reads .env again
        assert ensure_secret_key(restarted) == "file"
        assert get_settings().secret_key == key

    def test_a_secret_key_from_the_environment_is_never_written(self, env: Path) -> None:
        from app.config import ensure_secret_key

        assert ensure_secret_key() == "env"
        assert not (env / ".env").exists()

    def test_an_unwritable_env_file_falls_back_to_a_per_process_key(
        self, env: Path, monkeypatch
    ) -> None:
        from app.config import ConfigStore, ensure_secret_key, reset_store

        def _read_only(*_args, **_kwargs) -> None:
            raise PermissionError("read-only")

        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setattr(ConfigStore, "_atomic_write", _read_only)
        assert ensure_secret_key(reset_store(env_path=env / ".env")) == "ephemeral"
        assert len(get_settings().secret_key) > 20

    def test_starting_the_app_saves_a_generated_key(self, env: Path, monkeypatch) -> None:
        from app.config import parse_env_file, reset_store

        monkeypatch.delenv("SECRET_KEY", raising=False)
        reset_store(env_path=env / ".env")
        from app.main import create_app

        create_app()
        assert parse_env_file(env / ".env").get("SECRET_KEY")

    def test_the_soundcloud_connection_survives_a_restart(
        self, db: Session, env: Path, monkeypatch
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from app import config
        from app.services import soundcloud_service as sc

        monkeypatch.delenv("SECRET_KEY", raising=False)
        config.ensure_secret_key(config.reset_store(env_path=env / ".env"))
        expires = datetime.now(UTC) + timedelta(hours=1)
        sc.save_token(db, sc.TokenBundle("access", "refresh", expires))
        db.commit()

        # A restart: a fresh process has no per-process key and reads .env again.
        monkeypatch.setattr(config, "_EPHEMERAL_SECRET", None)
        config.reset_store(env_path=env / ".env")
        restored = sc.load_token(db)
        assert restored is not None
        assert restored.access_token == "access"


class TestPreferences:
    def test_defaults_are_used_when_nothing_is_stored(self, db: Session) -> None:
        prefs = load_preferences(db)
        assert prefs.ai_provider == "none", "no AI is the documented default"
        assert prefs.voice_id == ""
        assert prefs.tts_model_id == ""
        assert prefs.tts_ready is False, "the GUI must force an explicit voice/model choice"
        assert prefs.monthly_budget_usd == 25.0
        assert prefs.cost_confirm_threshold_usd == 5.0
        assert prefs.filename_style == "slug"
        assert prefs.soundcloud_sharing == "private"

    def test_tts_ready_requires_both_voice_and_model(self, db: Session) -> None:
        set_value(db, "elevenlabs_voice_id", "v1")
        assert load_preferences(db).tts_ready is False
        set_value(db, "elevenlabs_model_id", "eleven_multilingual_v2")
        assert load_preferences(db).tts_ready is True

    def test_values_round_trip(self, db: Session) -> None:
        set_many(db, {"monthly_budget_usd": 12.5, "ai_provider": "anthropic"})
        prefs = load_preferences(db)
        assert prefs.monthly_budget_usd == 12.5
        assert prefs.ai_provider == "anthropic"

    def test_unknown_keys_are_ignored(self, db: Session) -> None:
        set_many(db, {"totally_unknown": "x"})
        assert "totally_unknown" not in DEFAULTS

    def test_broken_numbers_fall_back(self, db: Session) -> None:
        set_value(db, "monthly_budget_usd", "not-a-number")
        assert get_float(db, "monthly_budget_usd", 99.0) == 99.0

    def test_invalid_filename_style_falls_back_to_slug(self, db: Session) -> None:
        set_value(db, "filename_style", "kaos")
        assert load_preferences(db).filename_style == "slug"

    def test_boolean_parsing(self, db: Session) -> None:
        set_value(db, "voice_use_speaker_boost", "1")
        assert get_bool(db, "voice_use_speaker_boost") is True
        set_value(db, "voice_use_speaker_boost", "0")
        assert get_bool(db, "voice_use_speaker_boost") is False


class TestAudioPaths:
    def _page(self, db: Session, title: str = "Introduksjon til KI", position: int = 1):
        course = make_course(db)
        module = make_module(db, course, "Modul 1 – Introduksjon", 1)
        page = make_page(db, course, module, title, position=position)
        db.commit()
        db.refresh(page)
        return page

    def test_layout_mirrors_the_canvas_structure(self, db: Session) -> None:
        page = self._page(db)
        assert (
            build_relative_path(page)
            == "mitt-emne/01-modul-1-introduksjon/01-introduksjon-til-ki.mp3"
        )

    def test_title_style_keeps_the_original_title(self, db: Session) -> None:
        page = self._page(db)
        assert build_relative_path(page, style="title").endswith("01 - Introduksjon til KI.mp3")

    def test_pages_without_a_module_get_their_own_folder(self, db: Session) -> None:
        course = make_course(db)
        page = make_page(db, course, None, "Løsblad", position=1)
        db.commit()
        assert build_relative_path(page).startswith("mitt-emne/00-uten-modul/")

    def test_writing_audio_returns_metadata(self, env: Path, db: Session) -> None:
        page = self._page(db)
        relative = build_relative_path(page)
        stored = write_audio(relative, b"ID3-some-bytes")

        assert stored.absolute_path.is_file()
        assert stored.size_bytes == len(b"ID3-some-bytes")
        assert stored.file_hash
        assert audio_exists(relative)
        # No stray temporary file is left behind.
        assert not list(stored.absolute_path.parent.glob("*.part"))

    def test_resolving_a_traversal_path_is_refused(self, env: Path) -> None:
        with pytest.raises((UnsafePathError, ValueError)):
            resolve_path("../../secrets.txt")

    def test_audio_exists_is_false_for_a_bad_path(self, env: Path) -> None:
        assert audio_exists("../escape.mp3") is False
        assert audio_exists("") is False


class TestFormatting:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0:00"), (9.4, "0:09"), (93.4, "1:33"), (3725, "1:02:05")],
    )
    def test_duration_formatting(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize(
        ("size", "expected"),
        [(0, "0 B"), (512, "512 B"), (2048, "2.0 kB"), (5 * 1024 * 1024, "5.0 MB")],
    )
    def test_byte_formatting(self, size: int, expected: str) -> None:
        assert format_bytes(size) == expected
