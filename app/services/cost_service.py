"""Assembling the pre-flight cost estimate shown before any paid job."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.pricing import estimate_pages, load_pricing, savings_vs_full_regeneration
from app.schemas.costs import EstimateRequest, EstimateResponse, PageCost
from app.services.app_settings import load_preferences
from app.services.presenters import spent_this_month
from app.services.selection import resolve_pages


def build_estimate(session: Session, request: EstimateRequest) -> EstimateResponse:
    """Estimate a job and decide whether it needs an extra confirmation."""
    prefs = load_preferences(session)
    pricing = load_pricing()

    ai_provider = request.ai_provider or prefs.ai_provider
    ai_model = request.ai_model or prefs.ai_model or pricing.default_model(ai_provider)
    tts_model = request.tts_model or prefs.tts_model_id

    pages = resolve_pages(session, mode=request.mode, page_ids=request.page_ids)

    estimate = estimate_pages(
        pages,
        ai_provider=ai_provider,
        ai_model=ai_model,
        tts_model=tts_model,
        force_regenerate=request.force_regenerate,
        pricing=pricing,
    )
    incremental_usd, full_usd = savings_vs_full_regeneration(
        pages, ai_provider=ai_provider, ai_model=ai_model, tts_model=tts_model, pricing=pricing
    )

    spent = spent_this_month(session)
    threshold = prefs.cost_confirm_threshold_usd
    budget = prefs.monthly_budget_usd
    total = estimate.total_usd

    return EstimateResponse(
        page_count=estimate.page_count,
        tts_page_count=estimate.tts_page_count,
        ai_page_count=estimate.ai_page_count,
        total_words=estimate.total_words,
        total_characters=estimate.total_characters,
        tts_characters=estimate.tts_characters,
        ai_provider=ai_provider,
        ai_model=ai_model,
        tts_model=tts_model,
        elevenlabs_usd=estimate.elevenlabs_usd,
        openai_usd=estimate.openai_usd,
        anthropic_usd=estimate.anthropic_usd,
        total_usd=total,
        currency_code=pricing.currency.code,
        currency_symbol=pricing.currency.symbol,
        total_local=pricing.to_local(total),
        full_regeneration_usd=full_usd,
        savings_usd=max(full_usd - incremental_usd, 0.0),
        requires_confirmation=bool(threshold > 0 and total > threshold),
        threshold_usd=threshold,
        budget_usd=budget,
        spent_this_month_usd=spent,
        exceeds_budget=bool(budget > 0 and spent + total > budget),
        pages=[
            PageCost(
                page_id=item.page_id,
                title=item.title,
                module_name=item.module_name,
                characters=item.characters,
                words=item.words,
                ai_provider=item.ai_provider,
                tts_model=item.tts_model,
                needs_ai=item.needs_ai,
                needs_tts=item.needs_tts,
                ai_cost_usd=item.ai_cost_usd,
                tts_cost_usd=item.tts_cost_usd,
                total_usd=item.total_usd,
            )
            for item in estimate.pages
        ],
    )
