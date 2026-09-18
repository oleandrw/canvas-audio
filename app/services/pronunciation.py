"""The user's own pronunciation list.

Text-to-speech mispronounces the things a course is full of: abbreviations like
«HiØ» and «jf.», symbols like «§», and local names. This is a plain list of
«from → to» that is applied while a Canvas page is cleaned, so the replaced text
is exactly the text shown on the page view, hashed for change detection, and sent
to the speech engine. There is no hidden second version of the text anywhere.

Because the list is applied at cleaning time, changing it takes effect when the
pages are cleaned again — which is what :func:`app.routes.pronunciation` does
right after saving, without calling Canvas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import PronunciationRule

#: A pattern longer than this is a mistake, not a pronunciation rule.
MAX_PATTERN = 200
MAX_REPLACEMENT = 400


@dataclass(frozen=True, slots=True)
class Rule:
    pattern: str
    replacement: str
    match_case: bool = False
    whole_word: bool = True


def load_rules(session: Session) -> list[Rule]:
    rows = session.execute(
        select(PronunciationRule)
        .where(PronunciationRule.enabled.is_(True))
        .order_by(PronunciationRule.position, PronunciationRule.id)
    ).scalars()
    return [
        Rule(
            pattern=row.pattern,
            replacement=row.replacement,
            match_case=row.match_case,
            whole_word=row.whole_word,
        )
        for row in rows
    ]


def _pattern_for(rules: list[Rule]) -> re.Pattern[str] | None:
    """One regex for the whole group, longest pattern first.

    Longest first so «NTNU Gjøvik» wins over «NTNU», and one single pass so a
    replacement can never be matched again by another rule.
    """
    if not rules:
        return None
    parts = []
    for rule in sorted(rules, key=lambda r: len(r.pattern), reverse=True):
        escaped = re.escape(rule.pattern)
        if rule.whole_word:
            # A boundary only means something next to a word character: «jf.»
            # gets one at the start and none at the end, and «§» gets neither.
            if rule.pattern[:1].isalnum():
                escaped = rf"\b{escaped}"
            if rule.pattern[-1:].isalnum():
                escaped = rf"{escaped}\b"
        parts.append(f"(?:{escaped})")
    return re.compile("|".join(parts))


def apply_rules(text: str, rules: list[Rule]) -> str:
    """Replace every match in one pass per case-sensitivity group."""
    if not text or not rules:
        return text

    for match_case in (True, False):
        group = [rule for rule in rules if rule.match_case is match_case and rule.pattern]
        pattern = _pattern_for(group)
        if pattern is None:
            continue
        if not match_case:
            pattern = re.compile(pattern.pattern, re.IGNORECASE)
        key = (lambda value: value) if match_case else str.casefold
        lookup = {key(rule.pattern): rule.replacement for rule in group}

        def replace(match: re.Match[str], lookup: dict[str, str] = lookup, key=key) -> str:  # noqa: ANN001
            return lookup.get(key(match.group(0)), match.group(0))

        text = pattern.sub(replace, text)
    return text


def clean_text(session: Session, text: str) -> str:
    """Apply the stored list. One database read, then pure text work."""
    return apply_rules(text, load_rules(session))


def list_rules(session: Session) -> list[PronunciationRule]:
    return list(
        session.execute(
            select(PronunciationRule).order_by(PronunciationRule.position, PronunciationRule.id)
        )
        .scalars()
        .all()
    )


def replace_all(session: Session, rows: list[dict[str, object]]) -> list[PronunciationRule]:
    """Store the list exactly as the user arranged it."""
    for existing in list_rules(session):
        session.delete(existing)
    session.flush()

    stored: list[PronunciationRule] = []
    for position, row in enumerate(rows, start=1):
        pattern = str(row.get("pattern") or "").strip()
        if not pattern:
            continue
        rule = PronunciationRule(
            pattern=pattern[:MAX_PATTERN],
            replacement=str(row.get("replacement") or "").strip()[:MAX_REPLACEMENT],
            match_case=bool(row.get("match_case")),
            whole_word=bool(row.get("whole_word", True)),
            enabled=bool(row.get("enabled", True)),
            note=str(row.get("note") or "").strip()[:200],
            position=position,
        )
        session.add(rule)
        stored.append(rule)
    session.flush()
    return stored
