"""Path safety, secret masking, log redaction and the optional app lock."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import ConfigStore, mask, parse_env_file
from app.logging_config import REDACTED, SecretRedactionFilter, redact
from app.security import TokenCipher, UnsafePathError, safe_join, verify_password


class TestSafeJoin:
    def test_joins_normal_segments(self, tmp_path: Path) -> None:
        result = safe_join(tmp_path, "emne", "01-modul", "01-side.mp3")
        assert result == (tmp_path / "emne" / "01-modul" / "01-side.mp3").resolve()

    @pytest.mark.parametrize(
        "segments",
        [
            ("..",),
            ("..", "..", "etc", "passwd"),
            ("emne", "..", "..", "secrets"),
            ("/etc/passwd",),
            ("C:\\Windows\\System32",),
            ("emne/../../..",),
        ],
    )
    def test_rejects_escapes(self, tmp_path: Path, segments: tuple[str, ...]) -> None:
        with pytest.raises((UnsafePathError, ValueError)):
            safe_join(tmp_path, *segments)

    def test_backslashes_are_treated_as_separators(self, tmp_path: Path) -> None:
        result = safe_join(tmp_path, "emne\\modul", "fil.mp3")
        assert result.parent.name == "modul"

    def test_empty_segments_are_ignored(self, tmp_path: Path) -> None:
        assert safe_join(tmp_path, "", ".", "fil.mp3") == (tmp_path / "fil.mp3").resolve()


class TestMasking:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", ""),
            ("short", "•••••"),
            ("sk-abcdefghijklmnop4f2a", "sk-…4f2a"),
        ],
    )
    def test_mask_never_reveals_the_middle(self, value: str, expected: str) -> None:
        assert mask(value) == expected

    def test_mask_of_a_long_key_is_short(self) -> None:
        assert len(mask("a" * 200)) < 12


class TestLogRedaction:
    def test_configured_secret_is_removed(self, env: Path, monkeypatch) -> None:
        monkeypatch.setenv("CANVAS_TOKEN", "12345~supersecrettoken1234567890")
        from app.config import reset_store

        reset_store(env_path=env / ".env")

        cleaned = redact("Calling Canvas with 12345~supersecrettoken1234567890 now")
        assert "supersecrettoken" not in cleaned
        assert REDACTED in cleaned

    @pytest.mark.parametrize(
        "line",
        [
            "Authorization: Bearer abcdef1234567890abcdef",
            "xi-api-key: sk_1234567890abcdefghijklmnop",
            "api_key=sk-abcdefghijklmnopqrstuvwxyz",
        ],
    )
    def test_credential_shaped_strings_are_removed(self, env: Path, line: str) -> None:
        cleaned = redact(line)
        assert REDACTED in cleaned
        assert "abcdef1234567890abcdef" not in cleaned or "sk-" not in cleaned

    def test_filter_rewrites_the_record(self, env: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-averylongopenaikeyvalue123")
        from app.config import reset_store

        reset_store(env_path=env / ".env")

        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "key is sk-averylongopenaikeyvalue123", (), None
        )
        SecretRedactionFilter().filter(record)
        assert "sk-averylongopenaikeyvalue123" not in record.getMessage()

    def test_ordinary_text_is_untouched(self, env: Path) -> None:
        assert redact("Fetched 8 modules") == "Fetched 8 modules"


class TestTokenCipher:
    def test_round_trips(self) -> None:
        cipher = TokenCipher("a-secret-key")
        assert cipher.decrypt(cipher.encrypt("hemmelig")) == "hemmelig"

    def test_ciphertext_does_not_contain_the_plaintext(self) -> None:
        cipher = TokenCipher("a-secret-key")
        assert "hemmelig" not in cipher.encrypt("hemmelig")

    def test_a_rotated_key_degrades_to_empty_not_a_crash(self) -> None:
        blob = TokenCipher("first-key").encrypt("hemmelig")
        assert TokenCipher("second-key").decrypt(blob) == ""

    def test_empty_values_pass_through(self) -> None:
        cipher = TokenCipher("k")
        assert cipher.encrypt("") == ""
        assert cipher.decrypt("") == ""


class TestConfigStore:
    def test_environment_beats_the_env_file(self, tmp_path: Path, monkeypatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("CANVAS_URL=https://from-file\n", encoding="utf-8")
        monkeypatch.setenv("CANVAS_URL", "https://from-environment")

        store = ConfigStore(env_file)
        assert store.get("CANVAS_URL") == "https://from-environment"
        assert store.source("CANVAS_URL") == "env"
        assert store.is_locked("CANVAS_URL")

    def test_file_is_used_when_the_environment_is_unset(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CANVAS_URL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("CANVAS_URL=https://from-file\n", encoding="utf-8")

        store = ConfigStore(env_file)
        assert store.get("CANVAS_URL") == "https://from-file"
        assert store.source("CANVAS_URL") == "file"
        assert not store.is_locked("CANVAS_URL")

    def test_writes_are_atomic_and_readable_again(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CANVAS_TOKEN", raising=False)
        store = ConfigStore(tmp_path / ".env")

        written, skipped = store.update({"CANVAS_TOKEN": "abc123"})
        assert written == ["CANVAS_TOKEN"]
        assert skipped == []
        assert parse_env_file(tmp_path / ".env")["CANVAS_TOKEN"] == "abc123"

    def test_env_managed_keys_are_never_written(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CANVAS_TOKEN", "from-environment")
        store = ConfigStore(tmp_path / ".env")

        written, skipped = store.update({"CANVAS_TOKEN": "attempted-overwrite"})
        assert written == []
        assert skipped == ["CANVAS_TOKEN"]
        assert not (tmp_path / ".env").exists()

    def test_unknown_keys_are_ignored(self, tmp_path: Path) -> None:
        store = ConfigStore(tmp_path / ".env")
        written, _ = store.update({"NOT_A_REAL_KEY": "x"})
        assert written == []

    def test_setting_an_empty_value_removes_the_key(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CANVAS_TOKEN", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("CANVAS_TOKEN=old\n", encoding="utf-8")

        store = ConfigStore(env_file)
        store.update({"CANVAS_TOKEN": ""})
        assert "CANVAS_TOKEN" not in parse_env_file(env_file)

    def test_quoted_values_are_unwrapped(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text('CANVAS_URL="https://quoted"\n# comment\nBROKEN\n', encoding="utf-8")
        assert parse_env_file(env_file) == {"CANVAS_URL": "https://quoted"}


class TestAppLock:
    def test_no_password_means_everything_is_allowed(self, env: Path) -> None:
        assert verify_password("")
        assert verify_password("anything")

    def test_password_must_match(self, env: Path, monkeypatch) -> None:
        monkeypatch.setenv("APP_PASSWORD", "riktig-passord")
        from app.config import reset_store

        reset_store(env_path=env / ".env")

        assert verify_password("riktig-passord")
        assert not verify_password("feil-passord")
