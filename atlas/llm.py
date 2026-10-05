"""Central LLM access.

Every model call the pipeline makes goes through :class:`LLMClient`. It:

* sends a JSON-mode chat completion,
* parses the reply into the requested Pydantic schema,
* on a validation failure, feeds the error back and retries,
* retries transient API errors with exponential backoff.

The underlying transport is injectable so tests can supply a fake client and
never touch the network.

Each call opens an optional Braintrust span carrying the model name, the prompt
*shape* (length plus a short stable hash -- never the prompt text), the token
counts the provider reports and the span's latency.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    """Raised when a structured call cannot produce a valid object."""


def _looks_like_json_mode_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text or "json mode" in text


class _ChatCompletions(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _Chat(Protocol):
    completions: _ChatCompletions


class _GroqLike(Protocol):
    chat: _Chat


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Handles fenced blocks and stray prose around the object.
    """
    if not text:
        raise ValueError("empty model response")

    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = "openai/gpt-oss-120b",
        temperature: float = 0.0,
        max_validation_retries: int = 3,
        max_api_retries: int = 3,
        client: _GroqLike | None = None,
    ) -> None:
        self.default_model = default_model
        self.temperature = temperature
        self.max_validation_retries = max_validation_retries
        self.max_api_retries = max_api_retries

        self._client: _GroqLike
        if client is not None:
            self._client = client
        else:
            from groq import Groq  # imported lazily so tests need no dependency

            if not api_key:
                raise LLMError("GROQ_API_KEY is not set. Add it to .env before running LLM calls.")
            self._client = cast(_GroqLike, Groq(api_key=api_key))

    # -- transport -------------------------------------------------------- #

    @staticmethod
    def _record_usage(response: Any, usage: dict[str, int]) -> None:
        """Accumulate token counts from ``response.usage`` into ``usage``.

        ``getattr`` rather than attribute access: a test fake is free to return a
        response with no usage at all, and a missing token counter must not turn
        into a failed call.
        """
        reported = getattr(response, "usage", None)
        if reported is None:
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(reported, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value

    def _create_once(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        *,
        json_mode: bool,
        usage: dict[str, int] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        if usage is not None:
            self._record_usage(response, usage)
        return response.choices[0].message.content or ""

    def _create(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        usage: dict[str, int] | None = None,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_api_retries + 1):
            try:
                return self._create_once(messages, model, temperature, json_mode=True, usage=usage)
            except Exception as exc:  # noqa: BLE001 - transport errors vary
                last_exc = exc
                # Not every model supports response_format=json_object. Fall back
                # to a plain call; the caller still validates the parsed JSON.
                if _looks_like_json_mode_error(exc):
                    try:
                        return self._create_once(
                            messages, model, temperature, json_mode=False, usage=usage
                        )
                    except Exception as fallback_exc:  # noqa: BLE001
                        last_exc = fallback_exc
                logger.warning(
                    "LLM call failed (attempt %s/%s): %s", attempt, self.max_api_retries, last_exc
                )
                if attempt < self.max_api_retries:
                    time.sleep(2 ** (attempt - 1))
        raise LLMError(f"LLM transport failed after {self.max_api_retries} attempts: {last_exc}")

    # -- structured call -------------------------------------------------- #

    def call_json(
        self,
        *,
        schema: type[TModel],
        system: str,
        user: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> TModel:
        """Call the model and validate the reply into ``schema``."""
        model = model or self.default_model
        temperature = self.temperature if temperature is None else temperature

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        from .observability import get_observability, prompt_fingerprint

        obs = get_observability()
        # Prompts carry the JD and resume fragments, so only their length and a
        # short stable hash are traced: enough to compare runs, not enough to
        # reconstruct one.
        metadata: dict[str, Any] = {
            "stage": "llm_call",
            "schema": schema.__name__,
            "model": model,
            "temperature": temperature,
        }
        metadata.update(prompt_fingerprint(system, "prompt_system"))
        metadata.update(prompt_fingerprint(user, "prompt_user"))
        span = obs.start_span(f"llm.call_json.{schema.__name__}", metadata=metadata)
        usage: dict[str, int] = {}

        last_error: str | None = None
        for attempt in range(1, self.max_validation_retries + 1):
            raw = self._create(messages, model, temperature, usage)
            try:
                payload = extract_json(raw)
                result = schema.model_validate(payload)
                span.set_metadata(validation_attempts=attempt, validation_ok=True)
                span.set_metadata(**usage)
                # The reply itself is not sent: a Verdict quotes the JD back.
                span.end()
                return result
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                # Only the exception's type: a validation message can quote the
                # model's reply, and replies are built from the JD.
                span.set_metadata(validation_ok=False, validation_error_type=type(exc).__name__)
                logger.warning(
                    "Validation failed for %s (attempt %s/%s): %s",
                    schema.__name__,
                    attempt,
                    self.max_validation_retries,
                    exc,
                )
                # Feed the failure back so the model can correct itself.
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That response did not validate against the required schema. "
                            f"Error: {exc}. Reply with ONLY a corrected JSON object that "
                            "matches the schema exactly."
                        ),
                    }
                )

        span.set_metadata(**usage)
        span.end()
        raise LLMError(
            f"Could not get a valid {schema.__name__} after "
            f"{self.max_validation_retries} attempts: {last_error}"
        )
