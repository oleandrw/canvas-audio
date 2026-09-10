"""Cost estimation.

The application must be able to tell the user what a job will probably cost
*before* spending money. All prices live in ``pricing.toml`` (see
:data:`app.config.Settings.pricing_file`) so they can be corrected without
touching application logic.

Everything produced here is an **estimate**. Actual usage is recorded separately
from provider responses in :class:`app.models.core.UsageRecord`.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models.core import AIProvider


@dataclass(frozen=True, slots=True)
class ModelPrice:
    model_id: str
    label: str
    input_per_mtok: float
    output_per_mtok: float
    supports_effort: bool = False
    supports_fallbacks: bool = False


@dataclass(frozen=True, slots=True)
class Currency:
    code: str
    symbol: str
    usd_rate: float


@dataclass(slots=True)
class Pricing:
    """Parsed ``pricing.toml``."""

    currency: Currency
    chars_per_token: float
    output_ratio: float
    tts_default_per_1k: float
    tts_models: dict[str, float]
    openai_models: dict[str, ModelPrice]
    anthropic_models: dict[str, ModelPrice]
    openai_default: str
    anthropic_default: str
    updated: str = ""

    # -- lookups ---------------------------------------------------------

    def tts_price_per_1k(self, model_id: str) -> float:
        return self.tts_models.get(model_id, self.tts_default_per_1k)

    def tts_cost(self, model_id: str, characters: int) -> float:
        return characters / 1000.0 * self.tts_price_per_1k(model_id)

    def ai_models(self, provider: str) -> dict[str, ModelPrice]:
        if provider == AIProvider.OPENAI:
            return self.openai_models
        if provider == AIProvider.ANTHROPIC:
            return self.anthropic_models
        return {}

    def ai_model(self, provider: str, model_id: str) -> ModelPrice | None:
        return self.ai_models(provider).get(model_id)

    def default_model(self, provider: str) -> str:
        if provider == AIProvider.OPENAI:
            return self.openai_default
        if provider == AIProvider.ANTHROPIC:
            return self.anthropic_default
        return ""

    def ai_cost(self, provider: str, model_id: str, input_tokens: int, output_tokens: int) -> float:
        price = self.ai_model(provider, model_id)
        if price is None:
            return 0.0
        return (
            input_tokens / 1_000_000 * price.input_per_mtok
            + output_tokens / 1_000_000 * price.output_per_mtok
        )

    def estimate_tokens(self, text_length: int) -> int:
        if self.chars_per_token <= 0:
            return 0
        return math.ceil(text_length / self.chars_per_token)

    def to_local(self, usd: float) -> float:
        return usd * self.currency.usd_rate


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

_cache: tuple[Path, float, Pricing] | None = None


def _model_price(model_id: str, raw: dict[str, Any]) -> ModelPrice:
    return ModelPrice(
        model_id=model_id,
        label=str(raw.get("label", model_id)),
        input_per_mtok=float(raw.get("input", 0.0)),
        output_per_mtok=float(raw.get("output", 0.0)),
        supports_effort=bool(raw.get("effort", False)),
        supports_fallbacks=bool(raw.get("fallbacks", False)),
    )


def parse_pricing(data: dict[str, Any]) -> Pricing:
    currency_raw = data.get("currency", {})
    estimation = data.get("estimation", {})
    elevenlabs = data.get("elevenlabs", {})
    openai_raw = data.get("openai", {})
    anthropic_raw = data.get("anthropic", {})

    openai_models = {
        model_id: _model_price(model_id, raw)
        for model_id, raw in openai_raw.get("models", {}).items()
    }
    anthropic_models = {
        model_id: _model_price(model_id, raw)
        for model_id, raw in anthropic_raw.get("models", {}).items()
    }

    return Pricing(
        currency=Currency(
            code=str(currency_raw.get("code", "USD")),
            symbol=str(currency_raw.get("symbol", "$")),
            usd_rate=float(currency_raw.get("usd_rate", 1.0)),
        ),
        chars_per_token=float(estimation.get("chars_per_token", 4.0)),
        output_ratio=float(estimation.get("output_ratio", 1.0)),
        tts_default_per_1k=float(elevenlabs.get("default_usd_per_1k_chars", 0.10)),
        tts_models={k: float(v) for k, v in elevenlabs.get("models", {}).items()},
        openai_models=openai_models,
        anthropic_models=anthropic_models,
        openai_default=str(openai_raw.get("default_model", "")),
        anthropic_default=str(anthropic_raw.get("default_model", "")),
        updated=str(data.get("meta", {}).get("updated", "")),
    )


def load_pricing(path: Path | None = None) -> Pricing:
    """Load and cache ``pricing.toml``, reloading automatically when it changes."""
    global _cache
    target = path or get_settings().pricing_file
    try:
        mtime = target.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _cache is not None and _cache[0] == target and _cache[1] == mtime:
        return _cache[2]

    if target.is_file():
        with target.open("rb") as handle:
            data = tomllib.load(handle)
    else:
        data = {}
    pricing = parse_pricing(data)
    _cache = (target, mtime, pricing)
    return pricing


def reset_pricing_cache() -> None:
    global _cache
    _cache = None


# --------------------------------------------------------------------------- #
# Estimates
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PageEstimate:
    page_id: int
    title: str
    module_name: str
    characters: int
    words: int
    needs_ai: bool
    needs_tts: bool
    ai_provider: str
    ai_model: str
    tts_model: str
    ai_input_tokens: int
    ai_output_tokens: int
    ai_cost_usd: float
    tts_cost_usd: float

    @property
    def total_usd(self) -> float:
        return self.ai_cost_usd + self.tts_cost_usd


@dataclass(slots=True)
class JobEstimate:
    pages: list[PageEstimate] = field(default_factory=list)
    currency: Currency = field(default_factory=lambda: Currency("USD", "$", 1.0))
    ai_provider: str = AIProvider.NONE
    ai_model: str = ""
    tts_model: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def ai_page_count(self) -> int:
        return sum(1 for p in self.pages if p.needs_ai)

    @property
    def tts_page_count(self) -> int:
        return sum(1 for p in self.pages if p.needs_tts)

    @property
    def total_characters(self) -> int:
        return sum(p.characters for p in self.pages)

    @property
    def total_words(self) -> int:
        return sum(p.words for p in self.pages)

    @property
    def tts_characters(self) -> int:
        return sum(p.characters for p in self.pages if p.needs_tts)

    @property
    def elevenlabs_usd(self) -> float:
        return sum(p.tts_cost_usd for p in self.pages)

    @property
    def openai_usd(self) -> float:
        return sum(p.ai_cost_usd for p in self.pages if p.ai_provider == AIProvider.OPENAI)

    @property
    def anthropic_usd(self) -> float:
        return sum(p.ai_cost_usd for p in self.pages if p.ai_provider == AIProvider.ANTHROPIC)

    @property
    def total_usd(self) -> float:
        return self.elevenlabs_usd + self.openai_usd + self.anthropic_usd

    def total_local(self) -> float:
        return self.total_usd * self.currency.usd_rate


def estimate_pages(
    pages: list[Any],
    *,
    ai_provider: str,
    ai_model: str,
    tts_model: str,
    force_regenerate: bool = False,
    pricing: Pricing | None = None,
) -> JobEstimate:
    """Estimate the cost of generating audio for ``pages``.

    A page contributes nothing to the estimate when its audio is already up to
    date and ``force_regenerate`` is false — that is the whole point of change
    detection. Likewise a page whose processed text is already cached for this
    exact provider/model does not contribute an AI cost.
    """
    from app.services.hashing import ai_result_is_reusable, audio_is_current

    pricing = pricing or load_pricing()
    estimate = JobEstimate(
        currency=pricing.currency,
        ai_provider=ai_provider,
        ai_model=ai_model,
        tts_model=tts_model,
    )

    for page in pages:
        needs_tts = force_regenerate or not audio_is_current(page)
        needs_ai = (
            ai_provider != AIProvider.NONE
            and needs_tts
            and not ai_result_is_reusable(page, ai_provider, ai_model)
        )

        characters = page.character_count
        words = page.word_count

        input_tokens = pricing.estimate_tokens(characters) if needs_ai else 0
        output_tokens = int(input_tokens * pricing.output_ratio) if needs_ai else 0
        ai_cost = (
            pricing.ai_cost(ai_provider, ai_model, input_tokens, output_tokens) if needs_ai else 0.0
        )
        tts_cost = pricing.tts_cost(tts_model, characters) if needs_tts else 0.0

        estimate.pages.append(
            PageEstimate(
                page_id=page.id,
                title=page.title,
                module_name=page.module.name if page.module else "Uten modul",
                characters=characters,
                words=words,
                needs_ai=needs_ai,
                needs_tts=needs_tts,
                ai_provider=ai_provider if needs_ai else AIProvider.NONE,
                ai_model=ai_model if needs_ai else "",
                tts_model=tts_model,
                ai_input_tokens=input_tokens,
                ai_output_tokens=output_tokens,
                ai_cost_usd=ai_cost,
                tts_cost_usd=tts_cost,
            )
        )
    return estimate


def savings_vs_full_regeneration(
    pages: list[Any],
    *,
    ai_provider: str,
    ai_model: str,
    tts_model: str,
    pricing: Pricing | None = None,
) -> tuple[float, float]:
    """Return ``(incremental_usd, full_usd)`` for the same page selection.

    Used for the "you save $X by only generating changed pages" line.
    """
    pricing = pricing or load_pricing()
    incremental = estimate_pages(
        pages,
        ai_provider=ai_provider,
        ai_model=ai_model,
        tts_model=tts_model,
        force_regenerate=False,
        pricing=pricing,
    )
    full = estimate_pages(
        pages,
        ai_provider=ai_provider,
        ai_model=ai_model,
        tts_model=tts_model,
        force_regenerate=True,
        pricing=pricing,
    )
    return incremental.total_usd, full.total_usd
