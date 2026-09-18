"""Schemas for the user's pronunciation list."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RuleIn(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    replacement: str = Field(default="", max_length=400)
    match_case: bool = False
    whole_word: bool = True
    enabled: bool = True
    note: str = Field(default="", max_length=200)

    @field_validator("pattern", "replacement", "note")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()

    @field_validator("pattern")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("Skriv hva som skal erstattes.")
        return value


class RulesIn(BaseModel):
    rules: list[RuleIn] = Field(default_factory=list, max_length=500)


class RuleOut(BaseModel):
    id: int
    pattern: str
    replacement: str
    match_case: bool
    whole_word: bool
    enabled: bool
    note: str


class RulesSaved(BaseModel):
    rules: list[RuleOut]
    changed_pages: int
    message: str


class SampleIn(BaseModel):
    text: str = Field(default="", max_length=5000)


class SampleOut(BaseModel):
    text: str
