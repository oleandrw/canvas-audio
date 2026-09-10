"""Small response shapes shared by several routes."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""


class TestResult(BaseModel):
    """Outcome of a "Test connection" button."""

    ok: bool
    provider: str
    detail: str = ""


class ErrorResponse(BaseModel):
    ok: bool = False
    provider: str = ""
    detail: str
    status_code: int | None = None


class Paginated(BaseModel):
    total: int = Field(default=0, ge=0)
