"""SQLAlchemy models.

Everything needed for change detection lives on :class:`Page` and
:class:`GeneratedAudio`: the Canvas timestamp, a hash of the cleaned original text
and a hash of the exact text that was sent to text-to-speech. Comparing those three
is what lets the app avoid paying for audio it already has.

Secrets are never stored here in plaintext — :class:`OAuthToken` holds Fernet
ciphertext and API keys are not stored in the database at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Status constants (plain strings keep SQLite migrations trivial)
# --------------------------------------------------------------------------- #


class PageAudioStatus:
    """Derived state of a page's audio, computed from hashes — never stored."""

    NOT_GENERATED = "not_generated"
    UP_TO_DATE = "up_to_date"
    CANVAS_CHANGED = "canvas_changed"
    TEXT_CHANGED = "text_changed"
    FAILED = "failed"


class ItemStatus:
    """Per-page state inside a generation job."""

    PENDING = "pending"
    CLEANING = "cleaning"
    AI_PROCESSING = "ai_processing"
    READY_FOR_TTS = "ready_for_tts"
    GENERATING_AUDIO = "generating_audio"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"

    TERMINAL = frozenset({DONE, SKIPPED, FAILED})


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = frozenset({COMPLETED, COMPLETED_WITH_ERRORS, FAILED, CANCELLED})


class AIProvider:
    NONE = "none"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


# --------------------------------------------------------------------------- #
# Canvas structure
# --------------------------------------------------------------------------- #


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_course_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(500))
    course_code: Mapped[str] = mapped_column(String(200), default="")
    slug: Mapped[str] = mapped_column(String(200), default="")
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    modules: Mapped[list[Module]] = relationship(
        back_populates="course", cascade="all, delete-orphan", order_by="Module.position"
    )
    pages: Mapped[list[Page]] = relationship(
        back_populates="course", cascade="all, delete-orphan", order_by="Page.position"
    )


class Module(Base):
    __tablename__ = "modules"
    __table_args__ = (UniqueConstraint("course_id", "canvas_module_id", name="uq_module_canvas"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    canvas_module_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer, default=0)
    slug: Mapped[str] = mapped_column(String(200), default="")

    course: Mapped[Course] = relationship(back_populates="modules")
    pages: Mapped[list[Page]] = relationship(
        back_populates="module", order_by="Page.position", passive_deletes=True
    )
    playlist: Mapped[SoundCloudPlaylist | None] = relationship(
        back_populates="module", uselist=False, cascade="all, delete-orphan"
    )


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("course_id", "canvas_page_id", name="uq_page_canvas"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    module_id: Mapped[int | None] = mapped_column(
        ForeignKey("modules.id", ondelete="SET NULL"), index=True, default=None
    )

    canvas_page_id: Mapped[str] = mapped_column(String(64), index=True)
    canvas_url: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer, default=0)
    canvas_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    html: Mapped[str] = mapped_column(Text, default="")
    original_text: Mapped[str] = mapped_column(Text, default="")
    original_hash: Mapped[str] = mapped_column(String(64), default="", index=True)

    processed_text: Mapped[str] = mapped_column(Text, default="")
    processed_hash: Mapped[str] = mapped_column(String(64), default="")
    processed_by: Mapped[str] = mapped_column(String(20), default=AIProvider.NONE)
    processed_model: Mapped[str] = mapped_column(String(100), default="")
    processed_source_hash: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    manually_edited: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Set when a page disappears from Canvas. The row is kept so the generated
    #: audio and its history are not silently lost.
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    last_error: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    course: Mapped[Course] = relationship(back_populates="pages")
    module: Mapped[Module | None] = relationship(back_populates="pages")
    audio: Mapped[GeneratedAudio | None] = relationship(
        back_populates="page", uselist=False, cascade="all, delete-orphan"
    )
    soundcloud_track: Mapped[SoundCloudTrack | None] = relationship(
        back_populates="page", uselist=False, cascade="all, delete-orphan"
    )
    canvas_embed: Mapped[CanvasEmbed | None] = relationship(
        back_populates="page", uselist=False, cascade="all, delete-orphan"
    )

    # -- derived helpers -------------------------------------------------

    @property
    def tts_text(self) -> str:
        """The text that would actually be sent to text-to-speech right now."""
        return self.processed_text or self.original_text

    @property
    def character_count(self) -> int:
        return len(self.tts_text)

    @property
    def word_count(self) -> int:
        return len(self.tts_text.split())


class GeneratedAudio(Base):
    __tablename__ = "generated_audio"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), unique=True, index=True
    )
    relative_path: Mapped[str] = mapped_column(String(1000))
    original_title: Mapped[str] = mapped_column(String(500), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    voice_id: Mapped[str] = mapped_column(String(100), default="")
    voice_name: Mapped[str] = mapped_column(String(200), default="")
    model_id: Mapped[str] = mapped_column(String(100), default="")
    voice_settings: Mapped[dict | None] = mapped_column(JSON, default=None)
    character_count: Mapped[int] = mapped_column(Integer, default=0)
    source_text_hash: Mapped[str] = mapped_column(String(64), default="")
    source_canvas_hash: Mapped[str] = mapped_column(String(64), default="")
    file_hash: Mapped[str] = mapped_column(String(64), default="")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    page: Mapped[Page] = relationship(back_populates="audio")


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED, index=True)

    ai_provider: Mapped[str] = mapped_column(String(20), default=AIProvider.NONE)
    ai_model: Mapped[str] = mapped_column(String(100), default="")
    voice_id: Mapped[str] = mapped_column(String(100), default="")
    voice_name: Mapped[str] = mapped_column(String(200), default="")
    tts_model: Mapped[str] = mapped_column(String(100), default="")
    voice_settings: Mapped[dict | None] = mapped_column(JSON, default=None)
    force_regenerate: Mapped[bool] = mapped_column(Boolean, default=False)

    total_items: Mapped[int] = mapped_column(Integer, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    skipped_items: Mapped[int] = mapped_column(Integer, default=0)

    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    items: Mapped[list[JobItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobItem.id"
    )

    @property
    def processed_items(self) -> int:
        return self.completed_items + self.failed_items + self.skipped_items

    @property
    def is_active(self) -> bool:
        return self.status not in JobStatus.TERMINAL


class JobItem(Base):
    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default=ItemStatus.PENDING, index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    characters: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    job: Mapped[GenerationJob] = relationship(back_populates="items")
    page: Mapped[Page] = relationship()


# --------------------------------------------------------------------------- #
# Usage / cost
# --------------------------------------------------------------------------- #


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    model: Mapped[str] = mapped_column(String(100), default="")
    operation: Mapped[str] = mapped_column(String(50), default="")
    characters: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    actual_cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    page_id: Mapped[int | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"), default=None
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# --------------------------------------------------------------------------- #
# SoundCloud
# --------------------------------------------------------------------------- #


class SoundCloudTrack(Base):
    __tablename__ = "soundcloud_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), unique=True, index=True
    )
    track_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    permalink_url: Mapped[str] = mapped_column(String(1000), default="")
    file_hash: Mapped[str] = mapped_column(String(64), default="")
    #: Earlier track ids for this page, space separated. When the audio is
    #: regenerated and uploaded again, playlists get the new id in their place.
    replaced_track_ids: Mapped[str] = mapped_column(Text, default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    page: Mapped[Page] = relationship(back_populates="soundcloud_track")


class SoundCloudPlaylist(Base):
    __tablename__ = "soundcloud_playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    module_id: Mapped[int] = mapped_column(
        ForeignKey("modules.id", ondelete="CASCADE"), unique=True, index=True
    )
    playlist_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    permalink_url: Mapped[str] = mapped_column(String(1000), default="")
    track_count: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    module: Mapped[Module] = relationship(back_populates="playlist")


# --------------------------------------------------------------------------- #
# Canvas audio player
# --------------------------------------------------------------------------- #


class CanvasEmbed(Base):
    """A page's audio uploaded to Canvas Files and shown in a player on the page."""

    __tablename__ = "canvas_embeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), unique=True, index=True
    )
    file_id: Mapped[str] = mapped_column(String(64), default="")
    file_hash: Mapped[str] = mapped_column(String(64), default="")
    folder_path: Mapped[str] = mapped_column(String(500), default="")
    design_fingerprint: Mapped[str] = mapped_column(String(32), default="")
    #: True while the player is on the Canvas page.
    embedded: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    page: Mapped[Page] = relationship(back_populates="canvas_embed")


# --------------------------------------------------------------------------- #
# Preferences and OAuth
# --------------------------------------------------------------------------- #


class AppSetting(Base):
    """Non-secret, GUI-editable preferences (budget, selected voice, ...)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OAuthToken(Base):
    """Encrypted OAuth tokens. Ciphertext only — see ``app.security.TokenCipher``."""

    __tablename__ = "oauth_tokens"

    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    scope: Mapped[str] = mapped_column(String(200), default="")
    account_name: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
