"""Cost estimation maths and the budget guard rails."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models.core import AIProvider
from app.pricing import (
    estimate_pages,
    load_pricing,
    parse_pricing,
    savings_vs_full_regeneration,
)
from app.schemas.costs import EstimateRequest
from app.services.app_settings import set_many
from app.services.cost_service import build_estimate
from tests.conftest import attach_audio, make_course, make_module, make_page

PRICING_DATA = {
    "currency": {"code": "NOK", "symbol": "kr", "usd_rate": 10.0},
    "estimation": {"chars_per_token": 4.0, "output_ratio": 1.0},
    "elevenlabs": {
        "default_usd_per_1k_chars": 0.10,
        "models": {"eleven_multilingual_v2": 0.10, "eleven_flash_v2_5": 0.05},
    },
    "openai": {
        "default_model": "gpt-test",
        "models": {"gpt-test": {"label": "GPT test", "input": 2.0, "output": 12.0}},
    },
    "anthropic": {
        "default_model": "claude-test",
        "models": {
            "claude-test": {"label": "Claude test", "input": 5.0, "output": 25.0, "effort": True}
        },
    },
}


@pytest.fixture()
def pricing():
    return parse_pricing(PRICING_DATA)


class TestPriceLookup:
    def test_known_tts_model_uses_its_own_price(self, pricing) -> None:
        assert pricing.tts_price_per_1k("eleven_flash_v2_5") == 0.05

    def test_unknown_tts_model_falls_back_to_default(self, pricing) -> None:
        assert pricing.tts_price_per_1k("eleven_unknown_v9") == 0.10

    def test_tts_cost_scales_per_1000_characters(self, pricing) -> None:
        assert pricing.tts_cost("eleven_multilingual_v2", 286_450) == pytest.approx(28.645)
        assert pricing.tts_cost("eleven_flash_v2_5", 286_450) == pytest.approx(14.3225)

    def test_ai_cost_uses_input_and_output_rates(self, pricing) -> None:
        cost = pricing.ai_cost(AIProvider.OPENAI, "gpt-test", 1_000_000, 1_000_000)
        assert cost == pytest.approx(14.0)

    def test_unknown_ai_model_costs_nothing_rather_than_guessing(self, pricing) -> None:
        assert pricing.ai_cost(AIProvider.OPENAI, "nope", 1000, 1000) == 0.0

    def test_local_currency_conversion(self, pricing) -> None:
        assert pricing.to_local(2.5) == pytest.approx(25.0)

    def test_token_estimate_rounds_up(self, pricing) -> None:
        assert pricing.estimate_tokens(10) == 3  # 10 / 4 = 2.5 → 3

    def test_effort_capability_is_config_driven(self, pricing) -> None:
        assert pricing.ai_model(AIProvider.ANTHROPIC, "claude-test").supports_effort
        assert not pricing.ai_model(AIProvider.OPENAI, "gpt-test").supports_effort


class TestJobEstimate:
    def _pages(self, db: Session, *, with_audio: bool = False):
        course = make_course(db)
        module = make_module(db, course, "Modul 1", 1)
        pages = [
            make_page(db, course, module, "Side A", position=1, text="a" * 1000),
            make_page(db, course, module, "Side B", position=2, text="b" * 2000),
        ]
        if with_audio:
            attach_audio(db, pages[0])
        db.commit()
        for page in pages:
            db.refresh(page)
        return pages

    def test_totals_add_up(self, db: Session, pricing) -> None:
        pages = self._pages(db)
        estimate = estimate_pages(
            pages,
            ai_provider=AIProvider.NONE,
            ai_model="",
            tts_model="eleven_multilingual_v2",
            pricing=pricing,
        )
        assert estimate.page_count == 2
        assert estimate.total_characters == 3000
        assert estimate.tts_characters == 3000
        assert estimate.elevenlabs_usd == pytest.approx(0.30)
        assert estimate.total_usd == pytest.approx(0.30)
        assert estimate.total_local() == pytest.approx(3.0)

    def test_pages_with_current_audio_are_free(self, db: Session, pricing) -> None:
        pages = self._pages(db, with_audio=True)
        estimate = estimate_pages(
            pages,
            ai_provider=AIProvider.NONE,
            ai_model="",
            tts_model="eleven_multilingual_v2",
            pricing=pricing,
        )
        assert estimate.tts_page_count == 1
        assert estimate.tts_characters == 2000
        assert estimate.total_usd == pytest.approx(0.20)

    def test_force_regenerate_charges_for_everything(self, db: Session, pricing) -> None:
        pages = self._pages(db, with_audio=True)
        estimate = estimate_pages(
            pages,
            ai_provider=AIProvider.NONE,
            ai_model="",
            tts_model="eleven_multilingual_v2",
            force_regenerate=True,
            pricing=pricing,
        )
        assert estimate.tts_page_count == 2
        assert estimate.total_usd == pytest.approx(0.30)

    def test_ai_cost_is_attributed_to_the_right_provider(self, db: Session, pricing) -> None:
        pages = self._pages(db)
        estimate = estimate_pages(
            pages,
            ai_provider=AIProvider.ANTHROPIC,
            ai_model="claude-test",
            tts_model="eleven_multilingual_v2",
            pricing=pricing,
        )
        assert estimate.ai_page_count == 2
        assert estimate.anthropic_usd > 0
        assert estimate.openai_usd == 0
        assert estimate.total_usd == pytest.approx(estimate.elevenlabs_usd + estimate.anthropic_usd)

    def test_per_page_costs_are_reported(self, db: Session, pricing) -> None:
        pages = self._pages(db)
        estimate = estimate_pages(
            pages,
            ai_provider=AIProvider.NONE,
            ai_model="",
            tts_model="eleven_multilingual_v2",
            pricing=pricing,
        )
        by_title = {p.title: p for p in estimate.pages}
        assert by_title["Side A"].tts_cost_usd == pytest.approx(0.10)
        assert by_title["Side B"].tts_cost_usd == pytest.approx(0.20)

    def test_savings_compares_incremental_against_full(self, db: Session, pricing) -> None:
        pages = self._pages(db, with_audio=True)
        incremental, full = savings_vs_full_regeneration(
            pages,
            ai_provider=AIProvider.NONE,
            ai_model="",
            tts_model="eleven_multilingual_v2",
            pricing=pricing,
        )
        assert incremental == pytest.approx(0.20)
        assert full == pytest.approx(0.30)
        assert full - incremental == pytest.approx(0.10)


class TestBudgetGuards:
    def _big_selection(self, db: Session) -> list[int]:
        course = make_course(db)
        module = make_module(db, course, "Modul 1", 1)
        pages = [
            make_page(db, course, module, f"Side {i}", position=i, text="x" * 20_000)
            for i in range(1, 6)
        ]
        db.commit()
        return [p.id for p in pages]

    def test_estimate_flags_confirmation_above_threshold(self, db: Session) -> None:
        page_ids = self._big_selection(db)
        set_many(db, {"cost_confirm_threshold_usd": "5", "monthly_budget_usd": "1000"})
        db.commit()

        estimate = build_estimate(
            db,
            EstimateRequest(page_ids=page_ids, mode="selected", tts_model="eleven_multilingual_v2"),
        )
        assert estimate.total_usd == pytest.approx(10.0)
        assert estimate.requires_confirmation
        assert not estimate.exceeds_budget

    def test_estimate_flags_budget_overrun(self, db: Session) -> None:
        page_ids = self._big_selection(db)
        set_many(db, {"cost_confirm_threshold_usd": "1000", "monthly_budget_usd": "5"})
        db.commit()

        estimate = build_estimate(
            db,
            EstimateRequest(page_ids=page_ids, mode="selected", tts_model="eleven_multilingual_v2"),
        )
        assert estimate.exceeds_budget

    def test_threshold_of_zero_disables_confirmation(self, db: Session) -> None:
        page_ids = self._big_selection(db)
        set_many(db, {"cost_confirm_threshold_usd": "0", "monthly_budget_usd": "0"})
        db.commit()

        estimate = build_estimate(
            db,
            EstimateRequest(page_ids=page_ids, mode="selected", tts_model="eleven_multilingual_v2"),
        )
        assert not estimate.requires_confirmation
        assert not estimate.exceeds_budget


def test_shipped_pricing_file_parses(env: Path) -> None:
    """The pricing.toml that ships with the repo must always be loadable."""
    pricing = load_pricing()
    assert pricing.currency.code
    assert pricing.tts_models
    assert pricing.anthropic_models
    assert pricing.openai_models
    assert pricing.default_model(AIProvider.ANTHROPIC) in pricing.anthropic_models
