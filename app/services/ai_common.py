"""Shared shape for the two optional AI text-processing providers."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.errors import InvalidContentError

#: Refuse absurdly long inputs rather than silently truncating them. A Canvas
#: page this long is a data problem, and truncating would drop content the user
#: expects to hear.
MAX_INPUT_CHARACTERS = 400_000


@dataclass(slots=True)
class AIResult:
    """Outcome of one text-processing call, with the usage needed for costing."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str


def guard_input_length(text: str) -> None:
    if len(text) > MAX_INPUT_CHARACTERS:
        raise InvalidContentError(
            f"Teksten er {len(text):,} tegn og for lang til AI-bearbeiding "
            f"(grense {MAX_INPUT_CHARACTERS:,}). Del opp Canvas-siden.".replace(",", " ")
        )


def estimate_max_output_tokens(input_characters: int) -> int:
    """Room for a rewrite of similar length, plus headroom for reasoning tokens."""
    approx_input_tokens = max(input_characters // 4, 256)
    return min(64_000, max(4_096, approx_input_tokens * 3 + 2_000))
