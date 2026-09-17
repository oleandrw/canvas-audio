"""Application entry point.

One FastAPI process serves both the JSON API and the HTML GUI, and owns the
single background worker that runs generation jobs.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.config import PROJECT_ROOT, ensure_secret_key, get_settings, get_store
from app.database import init_db, session_scope
from app.jobs.queue import get_queue
from app.logging_config import configure_logging, get_logger
from app.routes import (
    audio,
    canvas,
    canvas_player,
    costs,
    elevenlabs,
    generation,
    health,
    pages,
    soundcloud,
    ui,
    update,
)
from app.routes import (
    settings as settings_routes,
)
from app.services import app_settings, update_service
from app.services.errors import (
    AuthenticationError,
    ConfigurationError,
    InvalidContentError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    ServiceError,
    TransientError,
)

logger = get_logger("app.main")

#: Map service errors onto sensible HTTP status codes.
ERROR_STATUS: list[tuple[type[ServiceError], int]] = [
    (ConfigurationError, 400),
    (InvalidContentError, 400),
    (AuthenticationError, 502),
    (NotFoundError, 404),
    (RateLimitError, 429),
    (QuotaExceededError, 402),
    (TransientError, 504),
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    schema = init_db()
    with session_scope() as session:
        app_settings.record_schema_state(session, schema.version, schema.added_columns)

    logger.info("Canvas til lyd %s starting", __version__)
    logger.info("Database: %s", settings.database_url.split("///")[-1])
    logger.info("Output directory: %s", settings.output_dir)
    logger.info(
        "Credentials configured: canvas=%s elevenlabs=%s openai=%s anthropic=%s soundcloud=%s",
        bool(settings.canvas_url and settings.canvas_token),
        bool(settings.elevenlabs_api_key),
        bool(settings.openai_api_key),
        bool(settings.anthropic_api_key),
        bool(settings.soundcloud_client_id and settings.soundcloud_client_secret),
    )
    if schema.added_columns:
        logger.info("Database schema updated: %s", ", ".join(schema.added_columns))
    if not settings.auth_enabled:
        logger.info("APP_PASSWORD is not set - the GUI is open to anyone who can reach it.")

    # Remember which commit this process started from; the update page compares it
    # with the folder to see whether a restart is pending. Never fatal.
    with contextlib.suppress(Exception):
        await update_service.boot_commit()

    queue = get_queue()
    await queue.start()
    try:
        yield
    finally:
        await queue.stop()
        logger.info("Canvas til lyd stopped")


def create_app() -> FastAPI:
    secret_source = ensure_secret_key()
    settings = get_settings()
    configure_logging(settings.log_level)
    if secret_source == "generated":
        logger.info(
            "SECRET_KEY was not set: generated one and saved it to %s", get_store().env_path
        )
    elif secret_source == "ephemeral":
        logger.warning(
            "SECRET_KEY is not set and .env could not be written. Logins and the SoundCloud "
            "connection will not survive a restart; set SECRET_KEY in the environment."
        )

    app = FastAPI(
        title="Canvas → Lyd",
        description="Canvas LMS → renset tekst → valgfri AI → ElevenLabs → SoundCloud",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Signed session cookie: used for the optional app password and the
    # SoundCloud OAuth state/PKCE verifier. No CORS middleware — the GUI and the
    # API share an origin, so there is nothing to open up.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="canvas_audio_session",
        same_site="lax",
        https_only=settings.https_only,
        max_age=14 * 24 * 3600,
    )

    app.mount(
        "/static",
        StaticFiles(directory=str(PROJECT_ROOT / "app" / "static")),
        name="static",
    )

    app.include_router(health.router)
    app.include_router(canvas.router)
    app.include_router(pages.router)
    app.include_router(generation.router)
    app.include_router(elevenlabs.router)
    app.include_router(costs.router)
    app.include_router(audio.router)
    app.include_router(soundcloud.router)
    app.include_router(canvas_player.router)
    app.include_router(canvas_player.page_router)
    app.include_router(update.router)
    app.include_router(update.page_router)
    app.include_router(settings_routes.router)
    app.include_router(ui.router)

    register_exception_handlers(app)
    return app


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        status = 502
        for error_type, code in ERROR_STATUS:
            if isinstance(exc, error_type):
                status = code
                break
        logger.warning("Service error provider=%s status=%s: %s", exc.provider, status, exc.message)
        return JSONResponse(
            status_code=status,
            content={"ok": False, "provider": exc.provider, "detail": exc.message},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        message = first.get("msg", "Ugyldige data.")
        detail = f"{field}: {message}" if field else message
        return JSONResponse(status_code=422, content={"ok": False, "detail": detail})

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        # Covers UnsafePathError and friends without leaking a stack trace.
        logger.warning("Bad request: %s", exc)
        return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})


app = create_app()
