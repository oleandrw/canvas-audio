"""The user's pronunciation list: read it, replace it, and try it out."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_session
from app.logging_config import get_logger
from app.schemas.pronunciation import (
    RuleOut,
    RulesIn,
    RulesSaved,
    SampleIn,
    SampleOut,
)
from app.security import require_auth
from app.services import pronunciation, sync_service
from app.services.presenters import selected_course

logger = get_logger("app.routes.pronunciation")

router = APIRouter(prefix="/api/uttale", tags=["uttale"], dependencies=[Depends(require_auth)])


def _out(rows: list) -> list[RuleOut]:
    return [
        RuleOut(
            id=row.id,
            pattern=row.pattern,
            replacement=row.replacement,
            match_case=row.match_case,
            whole_word=row.whole_word,
            enabled=row.enabled,
            note=row.note,
        )
        for row in rows
    ]


@router.get("", response_model=list[RuleOut])
def list_rules(session: Session = Depends(get_session)) -> list[RuleOut]:
    return _out(pronunciation.list_rules(session))


@router.put("", response_model=RulesSaved)
def save_rules(payload: RulesIn, session: Session = Depends(get_session)) -> RulesSaved:
    """Store the list, then clean the stored pages again so it takes effect.

    Nothing is fetched from Canvas here — the pages are already stored, so this
    is local text work. Pages the list actually changes get a new hash and show
    up as «Teksten er endret», ready to be generated again.
    """
    stored = pronunciation.replace_all(session, [rule.model_dump() for rule in payload.rules])
    course = selected_course(session)
    changed = sync_service.reclean_course(session, course.id) if course else 0
    session.commit()

    logger.info("Pronunciation list saved rules=%d changed_pages=%d", len(stored), changed)
    if changed:
        message = (
            f"Lagret {len(stored)} oppslag. {changed} sider fikk ny tekst og er merket som endret."
        )
    else:
        message = f"Lagret {len(stored)} oppslag. Ingen sider ble endret av listen."
    return RulesSaved(rules=_out(stored), changed_pages=changed, message=message)


@router.post("/prov", response_model=SampleOut)
def try_rules(payload: SampleIn, session: Session = Depends(get_session)) -> SampleOut:
    """Show what a piece of text becomes. Costs nothing and stores nothing."""
    return SampleOut(text=pronunciation.clean_text(session, payload.text))
