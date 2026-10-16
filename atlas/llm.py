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
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    """Raised when a structured call cannot produce a valid object."""


class GroqDailyLimitReached(LLMError):
    """Raised when Groq reports the *daily* quota is spent.

    Distinct from a short per-minute throttle: sleeping will not help inside this
    run, so the caller stops cleanly and leaves the remaining jobs pending.
    """

    def __init__(self, message: str, reset_seconds: float | None = None) -> None:
        super().__init__(message)
        self.reset_seconds = reset_seconds


def _looks_like_json_mode_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text or "json mode" in text


def _looks_like_daily_limit(text: str) -> bool:
    """True when an error body names a per-day quota rather than a per-minute one."""
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "tokens per day",
            "requests per day",
            "per day",
            "tpd",
            "rpd",
            "daily limit",
            "daily quota",
        )
    )


#: A 429 with a Retry-After longer than this is treated as a spent daily quota
#: (per-minute windows reset in well under a minute), never as a short throttle.
DAILY_LIMIT_RETRY_AFTER_SECONDS = 120.0


_DURATION_RE = re.compile(r"(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")


def _retry_after_seconds(value: Any) -> float | None:
    """Parse a Retry-After / rate-limit-reset header into seconds.

    Accepts plain seconds (``"12"``), Groq's duration form (``"1m2.5s"``) and
    ``"0s"``. Returns ``None`` when the value is missing or unparseable.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    match = _DURATION_RE.fullmatch(text)
    if match and (match.group(1) or match.group(2)):
        minutes = float(match.group(1) or 0)
        seconds = float(match.group(2) or 0)
        return minutes * 60 + seconds
    return None


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

        # Observability hook: called with ``(seconds, reason)`` right before the
        # client sleeps (either to pace on the token budget or to back off after a
        # 429). The dashboard uses it to show "waiting for Groq rate limit". It is
        # a no-op when unset so the library never depends on a UI.
        self.on_wait: Callable[[float, str], None] | None = None
        # Last rate-limit headers seen on a response. ``None`` until the first
        # successful call, at which point proactive token pacing can begin.
        self._remaining_tokens: int | None = None
        self._limit_tokens: int | None = None
        self._tokens_reset: float = 0.0
        # Cumulative tokens (prompt + completion) the provider reported for every
        # successful response this client has made. The pipeline reads this to
        # enforce the per-run token cap and to record usage for the day.
        self.tokens_used: int = 0
        # Number of logical ``call_json`` invocations (parse/evaluate/verify/...),
        # not transport retries. The dashboard's add-progress page reads this to
        # show how many LLM calls a job has cost so far.
        self.calls: int = 0

        self._client: _GroqLike
        if client is not None:
            self._client = client
        else:
            from groq import Groq  # imported lazily so tests need no dependency

            if not api_key:
                raise LLMError("GROQ_API_KEY is not set. Add it to .env before running LLM calls.")
            self._client = cast(_GroqLike, Groq(api_key=api_key, max_retries=0))

    # -- transport -------------------------------------------------------- #

    def _record_usage(self, response: Any, usage: dict[str, int]) -> None:
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
        # The instance counter is cumulative across every response this client has
        # seen (all logical calls and every retry inside them). Add only this
        # response's own total so nothing is double-counted.
        total = getattr(reported, "total_tokens", None)
        if isinstance(total, int) and not isinstance(total, bool):
            self.tokens_used += total
            return
        prompt = getattr(reported, "prompt_tokens", None)
        completion = getattr(reported, "completion_tokens", None)
        if isinstance(prompt, int) and isinstance(completion, int):
            self.tokens_used += prompt + completion

    @staticmethod
    def _response_headers(response: Any) -> Any:
        """Best-effort access to a provider response's headers.

        The Groq SDK exposes them on the pydantic response (``response.headers``)
        or on the wrapped httpx response (``response._raw_response.headers``);
        fakes may expose neither, so every access is guarded.
        """
        for owner in (response, getattr(response, "_raw_response", None)):
            headers = getattr(owner, "headers", None)
            if headers:
                return headers
        return None

    def _read_rate_limit_headers(self, response: Any) -> None:
        headers = self._response_headers(response)
        if not headers:
            return

        def _int(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        remaining = _int("x-ratelimit-remaining-tokens")
        if remaining is not None:
            self._remaining_tokens = remaining
        limit = _int("x-ratelimit-limit-tokens")
        if limit is not None:
            self._limit_tokens = limit
        reset = headers.get("x-ratelimit-reset-tokens")
        seconds = _retry_after_seconds(reset)
        if seconds is not None:
            self._tokens_reset = seconds

    def _notify_wait(self, seconds: float, reason: str) -> None:
        if self.on_wait is not None:
            self.on_wait(max(0.0, float(seconds)), reason)

    def _pace(self, messages: list[dict[str, str]]) -> None:
        """Proactively wait on the token budget instead of waiting for a 429.

        After the first response the client knows ``x-ratelimit-remaining-tokens``.
        If that is below a rough estimate of the next call's cost, sleep until the
        window resets (capped) and tell the caller via ``on_wait``. With no header
        seen yet this is a no-op.
        """
        if self._remaining_tokens is None:
            return
        estimate = max(500, sum(len(m.get("content", "")) for m in messages) // 4)
        if self._remaining_tokens >= estimate:
            return
        wait = self._tokens_reset if self._tokens_reset > 0 else 5.0
        wait = min(wait, 65.0)
        self._notify_wait(wait, "token budget")
        time.sleep(wait)
        # Assume the window refilled; the next response's header will correct us.
        self._remaining_tokens = self._limit_tokens

    def _request(self, completions: Any, kwargs: dict[str, Any]) -> Any:
        """Send one chat completion, preferring the raw-response API.

        ``chat.completions.create`` returns a parsed object the Groq SDK strips of
        HTTP headers, so ``x-ratelimit-remaining-tokens`` is lost. The
        ``with_raw_response`` variant returns a wrapper exposing ``headers`` and a
        ``parse()`` for the same parsed object, so :meth:`_read_rate_limit_headers`
        can actually see the token budget. Transport fakes may implement only
        ``create``; fall back to it (and still read ``headers`` off the parsed
        object if the fake chose to expose any).
        """
        raw = getattr(completions, "with_raw_response", None)
        if raw is not None:
            response = raw.create(**kwargs)
            self._read_rate_limit_headers(response)
            return response.parse()
        response = completions.create(**kwargs)
        self._read_rate_limit_headers(response)
        return response

    def _create_once(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        *,
        json_mode: bool,
        usage: dict[str, int] | None = None,
    ) -> str:
        self._pace(messages)
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._request(self._client.chat.completions, kwargs)
        if usage is not None:
            self._record_usage(response, usage)
        return response.choices[0].message.content or ""

    def _create(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        usage: dict[str, int] | None = None,
        *,
        schema_name: str | None = None,
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
                exc_text = str(last_exc).lower() if last_exc else ""
                status_code = None
                retry_after = None
                try:
                    if last_exc is not None and hasattr(last_exc, "response"):
                        resp = last_exc.response
                        if resp is not None:
                            status_code = getattr(resp, "status_code", None)
                            headers = getattr(resp, "headers", {}) or {}
                            retry_after = headers.get("retry-after") or headers.get("Retry-After")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("failed to extract retry headers from LLM exception: %s", exc)
                effective_status = status_code or (
                    "429" if "rate limit" in exc_text or "429" in exc_text else None
                )
                parsed_retry = _retry_after_seconds(retry_after)
                # A long Retry-After or an explicit per-day message means the daily
                # quota is spent: sleeping cannot help inside this run, so stop now
                # and let the caller keep the remaining jobs pending.
                if _looks_like_daily_limit(exc_text) or (
                    parsed_retry is not None and parsed_retry > DAILY_LIMIT_RETRY_AFTER_SECONDS
                ):
                    hours = f"{parsed_retry / 3600:.1f}h" if parsed_retry else "a while"
                    logger.warning(
                        "Groq daily limit reached; stopping run (status=%s retry_after=%s)",
                        effective_status or "unknown",
                        retry_after or "-",
                    )
                    raise GroqDailyLimitReached(
                        f"Paused: Groq daily limit reached, resets in about {hours}",
                        reset_seconds=parsed_retry,
                    )
                try:
                    headers = (
                        self._response_headers(getattr(last_exc, "response", None))
                        if last_exc
                        else None
                    )
                    xremain = headers.get("x-ratelimit-remaining-tokens") if headers else None
                except Exception as exc_hdr:  # noqa: BLE001
                    logger.debug("failed to extract rl headers from last exception: %s", exc_hdr)
                    xremain = None
                logger.info(
                    "LLM retry %s/%s status=%s retry_after=%s model=%s call_type=%s x-rl-remaining-tokens=%s",
                    attempt,
                    self.max_api_retries,
                    effective_status or "unknown",
                    retry_after or "-",
                    model,
                    schema_name or "-",
                    xremain or "-",
                )
                if attempt < self.max_api_retries:
                    sleep = parsed_retry if parsed_retry is not None else 2 ** (attempt - 1)
                    capped = min(sleep, 60.0)
                    if str(effective_status) == "429" or parsed_retry is not None:
                        self._notify_wait(capped, "rate limit")
                    time.sleep(capped)
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
        self.calls += 1

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
            raw = self._create(messages, model, temperature, usage, schema_name=schema.__name__)
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
