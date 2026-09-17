"""Database engine and session handling.

Synchronous SQLAlchemy on purpose: it keeps the code readable, avoids the async
driver stack entirely, and SQLite is fast enough for a single-user tool. Route
handlers that only touch the database are declared ``def`` so FastAPI runs them in
its thread pool; the background worker wraps its database work in
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Engine, create_engine, event, inspect, literal
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None

T = TypeVar("T")


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        connect_args = {}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            future=True,
            pool_pre_ping=True,
        )
        if settings.database_url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for code outside the request cycle (jobs, startup)."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def run_db(func: Callable[[Session], T]) -> T:
    """Run a synchronous database callable from async code without blocking the loop."""

    def _runner() -> T:
        with session_scope() as session:
            return func(session)

    return await asyncio.to_thread(_runner)


#: Bumped by hand when a model gains a table or a column. There are no numbered
#: migrations — :func:`add_missing_columns` brings an older database up to date at
#: startup — so this is the label the update page shows, next to the columns that
#: were actually added.
SCHEMA_VERSION = 1


@dataclass(slots=True)
class SchemaState:
    """What :func:`init_db` did on this start."""

    version: int
    added_columns: list[str]


_last_schema_state: SchemaState | None = None


def init_db() -> SchemaState:
    """Create tables and add new columns. Simple enough that Alembic is not warranted."""
    global _last_schema_state
    from app.models import core  # noqa: F401  (import registers the mappers)

    engine = get_engine()
    core.Base.metadata.create_all(bind=engine)
    added = add_missing_columns(engine, core.Base.metadata)
    _stamp_schema_version(engine)
    _last_schema_state = SchemaState(version=SCHEMA_VERSION, added_columns=added)
    return _last_schema_state


def last_schema_state() -> SchemaState | None:
    """What the last :func:`init_db` in this process did, or ``None`` before it ran."""
    return _last_schema_state


def _stamp_schema_version(engine: Engine) -> None:
    """Write the version into the SQLite file, so a copied database still says what it is."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        # A module constant, never input, so the f-string is safe here.
        connection.exec_driver_sql(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


def file_schema_version() -> int | None:
    """The version stamped in the database file, or ``None`` when it is not SQLite."""
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return None
    with engine.connect() as connection:
        return int(connection.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def add_missing_columns(engine: Engine, metadata: Any) -> list[str]:
    """Add columns that models gained after an existing database was created.

    ``create_all`` only creates missing tables, so an older database would lack
    new columns. Only additions are handled, and a new column that cannot be
    empty needs a constant default so existing rows get a value.
    Returns the added columns as ``table.column``.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl_type = column.type.compile(dialect=engine.dialect)
                default = column.default.arg if column.default is not None else None
                if default is None or callable(default):
                    if not column.nullable:
                        raise RuntimeError(
                            f"Cannot add {table.name}.{column.name}: it needs a constant default"
                        )
                    clause = ""
                else:
                    rendered = literal(default).compile(
                        dialect=engine.dialect, compile_kwargs={"literal_binds": True}
                    )
                    clause = (
                        f" DEFAULT {rendered}"
                        if column.nullable
                        else f" NOT NULL DEFAULT {rendered}"
                    )
                connection.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}{clause}'
                )
                added.append(f"{table.name}.{column.name}")
    return added


def reset_engine() -> None:
    """Drop cached engine/session factory. Used by tests and after a config change."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
