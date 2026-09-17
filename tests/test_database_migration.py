"""Upgrading an existing database when models gain columns."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
)

from app.database import add_missing_columns


def test_a_new_column_is_added_with_its_default(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, track_id VARCHAR(64) NOT NULL)"
        )
        connection.exec_driver_sql("INSERT INTO tracks (id, track_id) VALUES (1, '42')")

    metadata = MetaData()
    Table(
        "tracks",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("track_id", String(64), nullable=False),
        Column("replaced_track_ids", Text, nullable=False, default=""),
    )

    assert add_missing_columns(engine, metadata) == ["tracks.replaced_track_ids"]
    with engine.connect() as connection:
        value = connection.exec_driver_sql("SELECT replaced_track_ids FROM tracks").scalar()
    assert value == "", "existing rows get the default"
    assert add_missing_columns(engine, metadata) == [], "running it again changes nothing"


def test_a_required_column_without_a_constant_default_is_refused(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tracks (id INTEGER PRIMARY KEY)")
    metadata = MetaData()
    Table(
        "tracks",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("uploaded_at", DateTime, nullable=False),
    )
    with pytest.raises(RuntimeError, match="constant default"):
        add_missing_columns(engine, metadata)


def test_the_schema_state_is_recorded_but_never_offered_to_the_settings_form(db) -> None:
    from app.services.app_settings import DEFAULTS, read_schema_state, record_schema_state

    record_schema_state(db, 3, ["pages.noe_nytt"])

    state = read_schema_state(db)
    assert state["version"] == 3
    assert state["added_columns"] == ["pages.noe_nytt"]
    assert state["checked_at"], "the page shows when the schema was last checked"
    for key in ("schema_version", "schema_checked_at", "schema_added_columns", "last_update"):
        assert key not in DEFAULTS, "system keys must not be postable from the settings form"


def test_init_db_upgrades_a_database_from_before_replaced_track_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import reset_store
    from app.database import (
        SCHEMA_VERSION,
        file_schema_version,
        get_engine,
        init_db,
        reset_engine,
    )

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'existing.db'}")
    reset_store(env_path=tmp_path / ".env")
    reset_engine()
    try:
        with get_engine().begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE soundcloud_tracks (id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, "
                "track_id VARCHAR(64) NOT NULL, title VARCHAR(500) NOT NULL, "
                "permalink_url VARCHAR(1000) NOT NULL, file_hash VARCHAR(64) NOT NULL, "
                "uploaded_at DATETIME NOT NULL)"
            )
        state = init_db()
        columns = {c["name"] for c in inspect(get_engine()).get_columns("soundcloud_tracks")}
        assert "replaced_track_ids" in columns
        assert state.added_columns == ["soundcloud_tracks.replaced_track_ids"]
        assert state.version == SCHEMA_VERSION
        assert file_schema_version() == SCHEMA_VERSION, "the version is stamped into the file"
    finally:
        reset_engine()
