"""Cost estimation and usage reporting."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.core import AIProvider


class EstimateRequest(BaseModel):
    page_ids: list[int] = Field(default_factory=list)
    mode: str = "selected"
    ai_provider: str = AIProvider.NONE
    ai_model: str = ""
    tts_model: str = ""
    force_regenerate: bool = False


class PageCost(BaseModel):
    page_id: int
    title: str
    module_name: str = ""
    characters: int
    words: int
    ai_provider: str = AIProvider.NONE
    tts_model: str = ""
    needs_ai: bool = False
    needs_tts: bool = False
    ai_cost_usd: float = 0.0
    tts_cost_usd: float = 0.0
    total_usd: float = 0.0


class EstimateResponse(BaseModel):
    """Always labelled as an estimate in the GUI — never presented as a fixed price."""

    page_count: int
    tts_page_count: int
    ai_page_count: int
    total_words: int
    total_characters: int
    tts_characters: int
    ai_provider: str
    ai_model: str = ""
    tts_model: str = ""

    elevenlabs_usd: float = 0.0
    openai_usd: float = 0.0
    anthropic_usd: float = 0.0
    total_usd: float = 0.0

    currency_code: str = "USD"
    currency_symbol: str = "$"
    total_local: float = 0.0

    full_regeneration_usd: float = 0.0
    savings_usd: float = 0.0

    requires_confirmation: bool = False
    threshold_usd: float = 0.0
    budget_usd: float = 0.0
    spent_this_month_usd: float = 0.0
    exceeds_budget: bool = False

    pages: list[PageCost] = Field(default_factory=list)


class ProviderUsage(BaseModel):
    provider: str
    characters: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


class UsageSummary(BaseModel):
    scope: str
    total_usd: float = 0.0
    total_local: float = 0.0
    currency_code: str = "USD"
    currency_symbol: str = "$"
    providers: list[ProviderUsage] = Field(default_factory=list)


class UsageResponse(BaseModel):
    this_job: UsageSummary | None = None
    this_month: UsageSummary
    all_time: UsageSummary
    budget_usd: float = 0.0
    budget_used_percent: int = 0
