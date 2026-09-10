"""Optional text processing with the Anthropic Messages API.

Streaming is used because a long Canvas page can produce a long response, and a
non-streaming request with a large ``max_tokens`` risks an HTTP timeout.

Model capabilities (``effort``, refusal ``fallbacks``) are read from
``pricing.toml`` rather than hardcoded, so adding a model is a config change.
"""

from __future__ import annotations

from typing import Any

from app.logging_config import get_logger
from app.pricing import load_pricing
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

logger = get_logger("app.services.anthropic")

PROVIDER = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _build_client(api_key: str) -> Any:
    import anthropic

    if not api_key:
        raise ConfigurationError("Anthropic API-nøkkel er ikke satt.", provider=PROVIDER)
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)


def _translate(exc: Exception) -> ServiceError:
    """Map SDK exceptions onto the app's error vocabulary."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return AuthenticationError(
            "Anthropic avviste API-nøkkelen.", provider=PROVIDER, status_code=401
        )
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitError("Anthropic begrenser antall forespørsler.", provider=PROVIDER)
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return TransientError("Fikk ikke kontakt med Anthropic.", provider=PROVIDER)
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        message = f"Anthropic svarte {status}: {getattr(exc, 'message', exc)}"
        if status == 402:
            return QuotaExceededError(message, provider=PROVIDER, status_code=status)
        if status and status >= 500:
            return TransientError(message, provider=PROVIDER, status_code=status)
        return UpstreamError(message, provider=PROVIDER, status_code=status)
    return UpstreamError(f"Uventet Anthropic-feil: {exc}", provider=PROVIDER)


def _extract_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
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

    pricing = load_pricing()
    model_price = pricing.ai_model(PROVIDER, model)
    owns_client = client is None
    client = client or _build_client(api_key)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": estimate_max_output_tokens(len(clean)),
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_message(title, clean)}],
    }
    if model_price is not None and model_price.supports_effort:
        # Straightforward rewriting: low effort keeps quality and cuts spend.
        kwargs["output_config"] = {"effort": "low"}

    use_beta = model_price is not None and model_price.supports_fallbacks
    if use_beta:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"

    logger.info("Sending page to Anthropic model=%s characters=%d", model, len(clean))

    try:
        endpoint = client.beta.messages if use_beta else client.messages
        async with endpoint.stream(**kwargs) as stream:
            message = await stream.get_final_message()
    except ServiceError:
        raise
    except Exception as exc:  # SDK exception surface is wide; translate them all
        raise _translate(exc) from exc
    finally:
        if owns_client:
            await client.close()

    if getattr(message, "stop_reason", None) == "refusal":
        raise UpstreamError(
            "Anthropic avslo å behandle denne teksten. Velg «Ingen AI» eller en annen "
            "leverandør for denne siden.",
            provider=PROVIDER,
        )

    result_text = _extract_text(message)
    if not result_text:
        raise UpstreamError("Anthropic returnerte tom tekst.", provider=PROVIDER)

    usage = getattr(message, "usage", None)
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
        models = await client.models.list(limit=1)
    except Exception as exc:
        raise _translate(exc) from exc
    finally:
        if owns_client:
            await client.close()

    count = len(getattr(models, "data", []) or [])
    return {
        "ok": True,
        "detail": f"Tilkoblet Anthropic ({count or 'flere'} modeller tilgjengelig).",
    }
