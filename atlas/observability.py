"""Optional Braintrust observability.

Every LLM call and every pipeline stage flows through this module, which gives
the run a Braintrust trace without changing any caller: when no API key is
configured, or the process is offline (CI, tests, ``ATLAS_OFFLINE``), every
helper here is a no-op, so those runs behave exactly as before.

Four rules govern what gets logged:

* **An allowlist decides what may leave the process.** Only the field names in
  :data:`ALLOWED_PAYLOAD_KEYS` survive; everything else is dropped by
  :func:`allowlist_payload` before a payload is handed to the SDK. The
  allowlist -- not the discipline of each call site -- is the control, so a
  future call site that tries to send a resume or a profile is filtered out.
* **Redaction is defence in depth.** :func:`redact`/:func:`redact_obj` still
  run over whatever the allowlist kept, so an allowed *value* that happens to
  carry an email, a phone number or a user path is masked as well.
* **Prompt text is never sent.** Prompts are reduced to a character count and a
  short stable hash (see :func:`prompt_fingerprint`), which keeps runs
  correlatable without leaking job or resume content.
* **Never raise.** A tracing failure is reported at warning level and the
  pipeline continues, so observability can never change a run's behaviour, exit
  code or eval result.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar
from urllib.parse import quote

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Project that receives Atlas traces and eval scores. Overridable with
#: ``BRAINTRUST_PROJECT``; the dashboard URL follows this name.
DEFAULT_BRAINTRUST_PROJECT = "My Project"

#: Project id that receives Atlas rows. Scopes every write to the owner-chosen
#: project so nothing can land in an unrelated one.
DEFAULT_BRAINTRUST_PROJECT_ID = "d7011a46-5b44-404c-a2d8-f0024bb40920"

#: Org that owns the project above. Login needs the *org* name, which is not the
#: project name, so the two are kept apart. Overridable with ``BRAINTRUST_ORG``.
DEFAULT_BRAINTRUST_ORG = "atlasops"

BRAINTRUST_APP_URL = "https://www.braintrust.dev/app"

#: Tags every published eval row carries, so the dashboard can be filtered to
#: Atlas eval scores alone.
EVAL_TAGS = ("atlas-ops", "eval")

#: Every field name Atlas may put on the wire. Grouped by what it describes so a
#: reviewer can see the boundary without reading the call sites: run/stage
#: identity, outcomes, scores, model and prompt identity, timing, token counts,
#: call diagnostics (shape only, never free text) and eval bookkeeping.
#:
#: Deliberately absent: the user's name, email and phone, resume text,
#: ``profile.json`` contents, prompt text, and every link except the job url.
ALLOWED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        # -- run and stage identity -----------------------------------------
        "stage",
        "run_id",
        "job_id",
        "job_title",
        "job_company",
        "job_url",
        # -- outcomes --------------------------------------------------------
        "decision",
        "recommendation",
        "status",
        "passed",
        "filter_passed",
        "needs_review",
        "evidence_failed",
        "rejection_reasons",
        "veto_reasons",
        # -- scores ----------------------------------------------------------
        "score",
        "scores",
        "match_score",
        "confidence",
        "fit_score",
        "must_have_coverage",
        "nice_to_have_coverage",
        # -- model and prompt identity --------------------------------------
        "model",
        "model_name",
        "model_names",
        "prompt_version",
        "prompt_versions",
        "schema",
        "temperature",
        # -- timing and token counts ----------------------------------------
        "latency_s",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_system_chars",
        "prompt_system_sha",
        "prompt_user_chars",
        "prompt_user_sha",
        # -- call diagnostics (shape only, no free text) ---------------------
        "validation_attempts",
        "validation_ok",
        "validation_error_type",
        "arg_count",
        "result_type",
        # -- eval bookkeeping ------------------------------------------------
        "experiment",
        "eval",
        "dataset",
        "k",
        "candidates",
        "top_k_selected",
        "year_correct",
        "year_checked",
        "golden_cases",
        "experience_pass_through",
        "false_rejects",
        "accepted",
        "skill_cases",
        "baseline_accuracy",
        "baseline_correct",
        "new_correct",
        "failures",
        "jobs",
        "processed",
        "filtered_out",
        "evaluated",
        "sent",
        "item_count",
        "quote_validation_failures",
    }
)

#: Allowlisted keys whose value may legitimately be a URL. Everything else is
#: still URL-redacted, so an allowlisted key cannot become a link smuggling
#: channel.
ALLOWED_URL_FIELDS: frozenset[str] = frozenset({"job_url"})

#: Score names and tags travel as dict keys / list entries. Restricting them to
#: an identifier shape stops free text being smuggled through a metric name.
_SAFE_METRIC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def braintrust_project() -> str:
    """Project name that receives Atlas traces."""
    return os.getenv("BRAINTRUST_PROJECT", "").strip() or DEFAULT_BRAINTRUST_PROJECT


def braintrust_project_id() -> str:
    """Project id that received the writes. Always paired with the project name."""
    return os.getenv("BRAINTRUST_PROJECT_ID", "").strip() or DEFAULT_BRAINTRUST_PROJECT_ID


def braintrust_org() -> str:
    """Org that owns :func:`braintrust_project_id`; what login needs."""
    return os.getenv("BRAINTRUST_ORG", "").strip() or DEFAULT_BRAINTRUST_ORG


def braintrust_dashboard_url() -> str:
    """Human-facing URL of the configured project. Never contains a key.

    The project name is percent-encoded: the owner-chosen name is
    ``My Project``, and a raw space makes the printed link unopenable.
    """
    return f"{BRAINTRUST_APP_URL}/{quote(braintrust_project())}"


def _safe_metric_name(name: str) -> bool:
    return bool(_SAFE_METRIC_RE.match(name))


def _short_hash(text: str) -> str:
    """Stable, non-reversible short digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def prompt_fingerprint(text: str, field: str = "prompt") -> dict[str, Any]:
    """Return ``{field}_chars`` and ``{field}_sha`` for ``text``.

    Two identical prompts produce the same fingerprint, so a run stays
    correlatable across time, while the prompt's content never leaves the
    process.
    """
    if not _safe_metric_name(field):
        logger.warning("Braintrust prompt fingerprint: unusable field name, nothing sent")
        return {}
    return {f"{field}_chars": len(text), f"{field}_sha": _short_hash(text)}


def allowlist_payload(payload: Any) -> Any:
    """Return ``payload`` with every key outside :data:`ALLOWED_PAYLOAD_KEYS` gone.

    Nested dicts are filtered too, which is what makes it safe to forward a
    model's parsed output as a span: ``Verdict.reasons_for`` and friends are
    dropped with their parent key, while allowlisted numbers survive.
    """
    if isinstance(payload, dict):
        kept: dict[str, Any] = {}
        dropped: list[str] = []
        for key, value in payload.items():
            name = str(key)
            if name in ALLOWED_PAYLOAD_KEYS:
                kept[name] = allowlist_payload(value)
            else:
                dropped.append(name)
        if dropped:
            logger.warning(
                "Braintrust payload: dropped non-allowlisted field(s): %s",
                ", ".join(sorted(dropped)),
            )
        return kept
    if isinstance(payload, (list, tuple)):
        return [allowlist_payload(item) for item in payload]
    return payload


def _allowed_scores(scores: Mapping[str, Any]) -> dict[str, float]:
    """Keep only numeric scores whose name is a plain identifier."""
    kept: dict[str, float] = {}
    dropped: list[str] = []
    for name, value in scores.items():
        usable = _safe_metric_name(str(name)) and isinstance(value, (int, float))
        if usable and not isinstance(value, bool):
            kept[str(name)] = value
        else:
            dropped.append(str(name))
    if dropped:
        logger.warning(
            "Braintrust scores: dropped unusable score name(s): %s", ", ".join(sorted(dropped))
        )
    return kept


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
    return f"{placeholder}_{_short_hash(raw)}"


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


def _redact_text(text: str, *, keep_urls: bool = False) -> str:
    out = text
    for placeholder, pattern in REDACTIONS:
        if keep_urls and placeholder == "<URL>":
            continue
        apply = _phone_redactor if placeholder == "<PHONE>" else _token_redactor(placeholder)
        out = apply(pattern, out)
    return out


def redact(text: str) -> str:
    """Replace PII spans in ``text`` with stable, non-reversible placeholders."""
    return _redact_text(text)


def _redact_obj(value: Any, *, keep_urls: bool) -> Any:
    if isinstance(value, str):
        return _redact_text(value, keep_urls=keep_urls)
    if isinstance(value, dict):
        return {k: _redact_obj(v, keep_urls=keep_urls) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_obj(v, keep_urls=keep_urls) for v in value]
    return value


def redact_obj(value: Any) -> Any:
    """Recursively redact strings inside dicts/lists; other types pass through."""
    return _redact_obj(value, keep_urls=False)


def secure_payload(payload: Any) -> Any:
    """Apply the allowlist, then redaction, to anything about to be sent.

    Order matters: redaction alone would mask the job url, and the allowlist
    alone would trust every value. Redaction keeps URLs only in the allowlisted
    URL fields (:data:`ALLOWED_URL_FIELDS`).
    """
    allowed = allowlist_payload(payload)
    if isinstance(allowed, dict):
        return {
            key: (value if key in ALLOWED_URL_FIELDS else _redact_obj(value, keep_urls=False))
            for key, value in allowed.items()
        }
    return _redact_obj(allowed, keep_urls=False)


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
        """Best-effort login. Returns True when tracing is active.

        A logger for the project is created here, not lazily at first span:
        ``start_span`` hands back a no-op span until the project is the active
        object, so deferring it would drop every stage trace silently.
        """
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
            braintrust.login(api_key=key, org_name=org_name or braintrust_org())
        except Exception as exc:  # noqa: BLE001 - never fail a run over telemetry
            logger.warning("Braintrust login failed, continuing without it: %s", exc)
            return False
        self._bt = braintrust
        # Kept only to hand the SDK its own credential on publish; never logged.
        self._api_key = key
        try:
            # A span needs an object to hang off: start_span returns a no-op span
            # when nothing has been logged to the project in this process, which
            # would silently drop every stage trace. init_logger also makes the
            # project the active parent object for spans started later on.
            braintrust.init_logger(
                project_id=braintrust_project_id(),
                api_key=key,
                async_flush=False,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a run over telemetry
            logger.warning("Braintrust logger unavailable, continuing without it: %s", exc)
            return False
        self.enabled = True
        logger.info(
            "Braintrust tracing enabled (project %s, org %s)",
            braintrust_project(),
            braintrust_org(),
        )
        return True

    def publish_score(
        self,
        name: str,
        value: float,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        extra_scores: Mapping[str, float] | None = None,
    ) -> bool:
        """Write one row carrying ``name`` (plus ``extra_scores``) as Braintrust scores.

        Returns True when a row reached the project. When observability is
        disabled this is a silent no-op: no SDK call, no output, no exception.
        Any SDK failure -- including a wrong signature or a wrong call -- is
        reported at warning level and returns False, because a broken publish
        must never fail an eval run or a pipeline run.
        """
        if not self.enabled:
            return False

        bt = self._bt
        tags = [*EVAL_TAGS, name] if _safe_metric_name(name) else list(EVAL_TAGS)
        try:
            scores = _allowed_scores({name: value, **(extra_scores or {})})
            log = bt.init_logger(
                project_id=braintrust_project_id(),
                api_key=self._api_key,
                async_flush=False,
            )
            log.log(
                input=secure_payload(input),
                output=secure_payload({"score": value}),
                scores=scores,
                metadata=secure_payload(dict(metadata or {})),
                tags=tags,
            )
            log.flush()
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail a run
            logger.warning("Braintrust publish of %s failed, row not recorded: %s", name, exc)
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
        self.metadata: dict[str, Any] = allowlist_payload(dict(metadata or {}))
        self.score: float | None = None
        self._handle: Any = None
        self._start = time.perf_counter()
        self.input = input
        if obs.enabled:
            try:
                self._handle = obs._bt.start_span(name=name, input=secure_payload(input))
            except Exception as exc:  # noqa: BLE001 - telemetry must never fail a run
                logger.warning("Braintrust span %s could not start: %s", name, exc)
                self._handle = None

    def set_metadata(self, **kwargs: Any) -> None:
        self.metadata.update(allowlist_payload(kwargs))

    def set_score(self, value: float) -> None:
        self.score = value

    def end(self, output: Any = None) -> None:
        elapsed = time.perf_counter() - self._start
        self.metadata["latency_s"] = round(elapsed, 4)
        if self.score is not None:
            self.metadata["score"] = self.score
        if self._handle is None:
            return
        # The SDK's Span.end takes only an end_time, so the event goes out via
        # log() first. The two calls are guarded separately: a span that cannot
        # record its result is still closed rather than left open.
        try:
            self._handle.log(output=secure_payload(output), metadata=secure_payload(self.metadata))
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail a run
            logger.warning("Braintrust span %s could not log its result: %s", self.name, exc)
        try:
            self._handle.end()
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail a run
            logger.warning("Braintrust span %s could not end: %s", self.name, exc)


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


@contextmanager
def traced_stage(name: str, **metadata: Any) -> Iterator[_Span]:
    """Span around one pipeline stage, as a context manager.

    This is the form the pipeline uses, because it gives the guarantee the
    pipeline needs: if the span cannot be started, or cannot be ended, the stage
    still runs. Both failures are reported at warning level and swallowed, so a
    tracing fault can never change what a stage computes.
    """
    try:
        span = get_observability().start_span(name, metadata=metadata)
    except Exception as exc:  # noqa: BLE001 - the stage must still run
        logger.warning("Braintrust span %s could not start, stage still runs: %s", name, exc)
        span = _Span(Observability(), name)
    try:
        yield span
    finally:
        try:
            span.end()
        except Exception as exc:  # noqa: BLE001 - the stage has already run
            logger.warning("Braintrust span %s could not end: %s", name, exc)


def traced(name: str | None = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorate a function so each call is logged as a Braintrust span.

    Neither the arguments nor the return value are sent: they can hold a profile
    or a job description, and the allowlist is not what stands between them and
    the wire. What is recorded is the shape of the call -- how many arguments, and
    the result type -- plus the span's latency.

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
            span = obs.start_span(span_name, metadata={"arg_count": len(args)})
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                span.set_metadata(validation_error_type=type(exc).__name__)
                span.end()
                raise
            span.set_metadata(result_type=type(result).__name__)
            span.end()
            return result

        return wrapper

    return decorator
