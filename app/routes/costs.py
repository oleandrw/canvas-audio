"""Cost estimation and recorded usage."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_session
from app.schemas.costs import EstimateRequest, EstimateResponse, UsageResponse
from app.security import require_auth
from app.services.app_settings import load_preferences
from app.services.cost_service import build_estimate
from app.services.presenters import month_start, spent_this_month, usage_summary

router = APIRouter(prefix="/api/costs", tags=["costs"], dependencies=[Depends(require_auth)])


@router.post("/estimate", response_model=EstimateResponse)
def estimate(payload: EstimateRequest, session: Session = Depends(get_session)) -> EstimateResponse:
    """Always an estimate — never presented to the user as a guaranteed price."""
    return build_estimate(session, payload)


@router.get("/usage", response_model=UsageResponse)
def usage(
    job_id: int | None = Query(default=None), session: Session = Depends(get_session)
) -> UsageResponse:
    prefs = load_preferences(session)
    spent = spent_this_month(session)
    budget = prefs.monthly_budget_usd

    return UsageResponse(
        this_job=(
            usage_summary(session, scope="job", job_id=job_id) if job_id is not None else None
        ),
        this_month=usage_summary(session, scope="month", since=month_start()),
        all_time=usage_summary(session, scope="all"),
        budget_usd=budget,
        budget_used_percent=int(min(spent / budget * 100, 999)) if budget > 0 else 0,
    )
