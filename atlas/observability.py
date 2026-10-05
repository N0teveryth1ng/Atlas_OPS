"""Optional Braintrust observability.

Every LLM call in the pipeline flows through :class:`atlas.llm.LLMClient`. This
module gives that single chokepoint a Braintrust trace without changing any
caller: when no API key is configured, every helper here is a no-op, so tests,
``selfcheck`` and offline runs behave exactly as before.

Three rules govern what gets logged:

* **PII is redacted before it leaves the process.** The pipeline reads real
  resumes and real job descriptions. Phone numbers, emails and Windows user
  paths are replaced with stable placeholders before any payload is sent.
* **Never raise.** Observability must not be able to fail a pipeline run, so
  every Braintrust interaction is wrapped and logged at debug level.
* **A broken publish must be loud.** :meth:`Observability.publish_score` catches
  only the SDK's own ``BraintrustAPIError`` hierarchy and reports it at error
  level with the original exception. Anything else — a wrong signature, a wrong
  call, a bug — propagates instead of masquerading as a silent no-op.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Fallback BrainTrust project/org. Overridable with ``BRAINTRUST_PROJECT``.
DEFAULT_BRAINTRUST_PROJECT = "atlasops"

#: Project id that receives Atlas rows. Scopes every write to the owner-chosen
#: project so nothing can land in an unrelated one.
DEFAULT_BRAINTRUST_PROJECT_ID = "3c5416f9-ff13-4c16-9a07-71d0e5a8c090"

BRAINTRUST_APP_URL = "https://www.braintrust.dev/app"

#: Tags every published eval row carries, so the dashboard can be filtered to
#: Atlas eval scores alone.
EVAL_TAGS = ("atlas-ops", "eval")


def braintrust_project() -> str:
    """Project/org name that receives Atlas traces."""
    return os.getenv("BRAINTRUST_PROJECT", "").strip() or DEFAULT_BRAINTRUST_PROJECT


def braintrust_project_id() -> str:
    """Project id that received the writes. Always paired with the project name."""
    return os.getenv("BRAINTRUST_PROJECT_ID", "").strip() or DEFAULT_BRAINTRUST_PROJECT_ID


def braintrust_dashboard_url() -> str:
    """Human-facing URL of the configured project. Never contains a key."""
    return f"{BRAINTRUST_APP_URL}/{braintrust_project()}"


#: Text that must never reach Braintrust. Each pattern maps to a placeholder
#: that is stable for the same input, so redacted spans stay correlatable
#: across runs without being readable.
REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("<EMAIL>", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")),
    (
        "<PHONE>",
        re.compile(
            r"(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3,4}[\s-]?\d{3,4}(?:[\s-]?\d{2,4})?"
        ),
    ),
    ("<PATH>", re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+")),
    ("<URL>", re.compile(r"https?://\S+")),
)

#: Phone pattern is greedy enough to eat ordinary numbers, so only redact runs
#: that look like real phone numbers (7+ digits with at least one separator).
_PHONE_MIN_DIGITS = 7

#: Truthy env values that must suppress all outbound telemetry. Tests set this
#: so an ``.env`` left on a dev machine can never leak into CI.
_OFFLINE_ENV_VARS = ("ATLAS_OFFLINE", "PYTEST_CURRENT_TEST", "CI")


def _in_offline_mode() -> bool:
    """True when the process must not emit telemetry, whatever ``.env`` says."""
    return any(os.getenv(var) for var in _OFFLINE_ENV_VARS)


def _looks_like_phone(candidate: str) -> bool:
    digits = [c for c in candidate if c.isdigit()]
    return len(digits) >= _PHONE_MIN_DIGITS


def _stable_token(placeholder: str, raw: str) -> str:
    """Return a stable, non-reversible stand-in for ``raw``."""
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{placeholder}_{digest}"


def _redact_phone(match: re.Match[str]) -> str:
    raw = match.group(0)
    return _stable_token("<PHONE>", raw) if _looks_like_phone(raw) else raw


def _phone_redactor(pattern: re.Pattern[str], text: str) -> str:
    """Apply :func:`_redact_phone` across ``text``."""
    return pattern.sub(_redact_phone, text)


def _token_redactor(token: str) -> Callable[[re.Pattern[str], str], str]:
    """Build a redaction callable with ``token`` already bound.

    Binding ``token`` here, instead of closing over the loop variable inside a
    lambda, is what stops one iteration's placeholder leaking into the next.
    """

    def apply(pattern: re.Pattern[str], text: str) -> str:
        return pattern.sub(lambda m: _stable_token(token, m.group(0)), text)

    return apply


def redact(text: str) -> str:
    """Replace PII spans in ``text`` with stable, non-reversible placeholders."""
    out = text
    for placeholder, pattern in REDACTIONS:
        apply = _phone_redactor if placeholder == "<PHONE>" else _token_redactor(placeholder)
        out = apply(pattern, out)
    return out


def redact_obj(value: Any) -> Any:
    """Recursively redact strings inside dicts/lists; other types pass through."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


class Observability:
    """Thin wrapper over the Braintrust SDK.

    Constructed with :func:`get_observability`. When the SDK is missing or no
    API key is set, every method is a no-op and :attr:`enabled` is ``False``.
    """

    def __init__(self) -> None:
        self.enabled = False
        self._bt: Any = None
        self._api_key: str | None = None

    def init(self, *, api_key: str | None = None, org_name: str | None = None) -> bool:
        """Best-effort login. Returns True when tracing is active."""
        key = api_key or os.getenv("BRAINTRUST_API_KEY", "").strip()
        if not key:
            logger.debug("Braintrust disabled: BRAINTRUST_API_KEY not set")
            return False
        if _in_offline_mode():
            logger.debug("Braintrust disabled: offline/test mode")
            return False
        try:
            import braintrust
        except ImportError:
            logger.warning("Braintrust disabled: package not installed")
            return False
        try:
            braintrust.login(api_key=key, org_name=org_name or braintrust_project())
        except Exception as exc:  # noqa: BLE001 - never fail a run over telemetry
            logger.warning("Braintrust login failed, continuing without it: %s", exc)
            return False
        self._bt = braintrust
        # Kept only to hand the SDK its own credential on publish; never logged.
        self._api_key = key
        self.enabled = True
        logger.info("Braintrust tracing enabled (project %s)", braintrust_project())
        return True

    def publish_score(
        self,
        name: str,
        value: float,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Write one row carrying ``name`` as a Braintrust score.

        Returns True when a row reached the project. When observability is
        disabled this is a silent no-op: no SDK call, no output, no exception.
        On an SDK error the exception is reported at error level rather than
        swallowed, because a silent no-op must mean "telemetry off", never
        "telemetry broken".
        """
        if not self.enabled:
            return False

        bt = self._bt
        try:
            log = bt.init_logger(
                project_id=braintrust_project_id(),
                api_key=self._api_key,
                async_flush=False,
            )
            log.log(
                input=redact_obj(input),
                output={"score": value},
                scores={name: value},
                metadata=redact_obj(dict(metadata or {})),
                tags=[*EVAL_TAGS, name],
            )
            log.flush()
        except bt.api.BraintrustAPIError as exc:
            logger.error("Braintrust publish of %s failed, row not recorded: %s", name, exc)
            return False

        print(f"Braintrust: published {name} -> {braintrust_dashboard_url()}")
        return True

    def start_span(
        self,
        name: str,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> _Span:
        return _Span(self, name, input=input, metadata=metadata)


class _Span:
    """Context-manager-ish span that flushes on :meth:`end`."""

    def __init__(
        self,
        obs: Observability,
        name: str,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.obs = obs
        self.name = name
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.score: float | None = None
        self._handle: Any = None
        self._start = time.perf_counter()
        self.input = input
        if obs.enabled:
            try:
                self._handle = obs._bt.start_span(name=name, input=redact_obj(input))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Braintrust span start failed for %s: %s", name, exc)
                self._handle = None

    def set_metadata(self, **kwargs: Any) -> None:
        self.metadata.update(redact_obj(kwargs))

    def set_score(self, value: float) -> None:
        self.score = value

    def end(self, output: Any = None) -> None:
        elapsed = time.perf_counter() - self._start
        self.metadata["latency_s"] = round(elapsed, 4)
        if self.score is not None:
            self.metadata["score"] = self.score
        if self._handle is None:
            return
        meta = dict(self.metadata)
        try:
            self._handle.end(output=redact_obj(output), metadata=meta)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Braintrust span end failed for %s: %s", self.name, exc)


_OBS: Observability | None = None


def get_observability() -> Observability:
    """Return the process-wide observability singleton, initialising on first use.

    Initialisation is what logs in and flips :attr:`Observability.enabled`. Doing
    it here rather than at each call site means every entry point -- LLM spans and
    eval score publishing alike -- gets telemetry from the same key, and offline
    runs still short-circuit inside :meth:`Observability.init`.
    """
    global _OBS
    if _OBS is None:
        obs = Observability()
        obs.init()
        _OBS = obs
    return _OBS


def reset_observability() -> None:
    """Drop the cached singleton. Used by tests."""
    global _OBS
    _OBS = None


def traced(name: str | None = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorate a function so each call is logged as a Braintrust span.

    Exceptions are recorded on the span and re-raised untouched, so tracing can
    never change control flow.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            obs = get_observability()
            if not obs.enabled:
                return fn(*args, **kwargs)
            span = obs.start_span(span_name, input={"args": list(args), "kwargs": kwargs})
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                span.set_metadata(error=repr(exc))
                span.end(output=None)
                raise
            span.end(output=result)
            return result

        return wrapper

    return decorator
