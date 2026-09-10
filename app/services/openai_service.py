"""Optional text processing with the OpenAI Responses API.

Kept deliberately parallel to :mod:`app.services.anthropic_service` so the job
pipeline can treat the two providers identically.
"""

from __future__ import annotations

from typing import Any

from app.logging_config import get_logger
from app.services.ai_common import AIResult, estimate_max_output_tokens, guard_input_length
from app.services.errors import (
    AuthenticationError,
    ConfigurationError,
    QuotaExceededError,
    RateLimitError,
    ServiceError,
    TransientError,
    UpstreamError,
)
from app.services.prompts import SYSTEM_PROMPT, build_user_message
from app.services.text_service import ensure_plain_text

logger = get_logger("app.services.openai")

PROVIDER = "openai"


def _build_client(api_key: str) -> Any:
    import openai

    if not api_key:
        raise ConfigurationError("OpenAI API-nøkkel er ikke satt.", provider=PROVIDER)
    return openai.AsyncOpenAI(api_key=api_key, max_retries=3)


def _translate(exc: Exception) -> ServiceError:
    import openai

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return AuthenticationError(
            "OpenAI avviste API-nøkkelen.", provider=PROVIDER, status_code=401
        )
    if isinstance(exc, openai.RateLimitError):
        return RateLimitError("OpenAI begrenser antall forespørsler.", provider=PROVIDER)
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return TransientError("Fikk ikke kontakt med OpenAI.", provider=PROVIDER)
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        message = f"OpenAI svarte {status}: {getattr(exc, 'message', exc)}"
        if status in (402, 413):
            return QuotaExceededError(message, provider=PROVIDER, status_code=status)
        if status and status >= 500:
            return TransientError(message, provider=PROVIDER, status_code=status)
        return UpstreamError(message, provider=PROVIDER, status_code=status)
    return UpstreamError(f"Uventet OpenAI-feil: {exc}", provider=PROVIDER)


def _extract_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    # Fall back to walking the output blocks if the convenience field is absent.
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for block in getattr(item, "content", []) or []:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                parts.append(block_text)
    return "\n".join(parts).strip()


async def process_text(
    text: str,
    *,
    title: str,
    model: str,
    api_key: str,
    client: Any | None = None,
) -> AIResult:
    """Rewrite ``text`` for narration. Returns the text plus real token usage."""
    clean = ensure_plain_text(text, context="teksten som skal bearbeides")
    guard_input_length(clean)

    owns_client = client is None
    client = client or _build_client(api_key)

    logger.info("Sending page to OpenAI model=%s characters=%d", model, len(clean))

    try:
        response = await client.responses.create(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=build_user_message(title, clean),
            max_output_tokens=estimate_max_output_tokens(len(clean)),
        )
    except ServiceError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    finally:
        if owns_client:
            await client.close()

    result_text = _extract_text(response)
    if not result_text:
        raise UpstreamError("OpenAI returnerte tom tekst.", provider=PROVIDER)

    usage = getattr(response, "usage", None)
    return AIResult(
        text=result_text,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        model=model,
        provider=PROVIDER,
    )


async def check_connection(api_key: str, *, client: Any | None = None) -> dict[str, Any]:
    """Cheap, unbilled check used by the Settings page."""
    owns_client = client is None
    client = client or _build_client(api_key)
    try:
        models = await client.models.list()
        count = len(getattr(models, "data", []) or [])
    except Exception as exc:
        raise _translate(exc) from exc
    finally:
        if owns_client:
            await client.close()

    return {"ok": True, "detail": f"Tilkoblet OpenAI ({count or 'flere'} modeller tilgjengelig)."}
