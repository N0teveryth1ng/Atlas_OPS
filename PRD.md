# PRD.md

## Purpose
Fresher (3 months internship); precision over recall; shortlist surfaced in the
local dashboard (email delivery cancelled).

## Pipeline
profile, query planner, collectors, normalize/dedupe, JD parser (regex+LLM), hard
filters, skill matching, evaluator, adversarial verifier, ranker, decision engine
(APPLY/REVIEW/SKIP), digest, dashboard, feedback, selfcheck, eval, label-check.

## Rules
experience tolerance 1 year; hard filters in code (deterministic first, LLM second);
status machine + sent subset of passed; evidence-quote validation; separate verifier
model; every surfaced job carries a match reason and every rejection logs a reason.

## Interface
CLI + scheduled run + local web dashboard (`/setup`, `/profile`, Find jobs, results,
`/rejected`, owner-only `/add` with a `/add/progress` polling page). Dashboard is
password-guarded and refuses to bind non-loopback without `DASHBOARD_PASSWORD`.
Email delivery cancelled (local digest only).

## Sources
Remotive, RemoteOK, Himalayas (multi-page search). Adzuna (no keys configured),
Greenhouse/Lever/Ashby (code exists; may be unreachable/unkeyed). YC jobs source is
CLOSED by owner (ToS prohibits automated reading); see HANDOFF.md for alternatives.

## Cost / budget controls
Deterministic pre-filter rejects before any LLM call; per-run caps
(`max_jobs_per_run: 12`, `max_llm_calls_per_run: 100`,
`max_llm_tokens_per_run: 120000`); per-day cap
(`max_llm_tokens_per_day: 200000`). A Groq daily-limit 429 or the per-run token cap
PAUSES the run (jobs stay resumable). Find jobs refuses to start when today's tokens
are >= 90% of the daily cap or a daily-limit pause is still in effect. The Groq SDK
is built with `max_retries=0`: all retrying, pacing and daily-limit detection live
in our own wrapper.

## Non-goals
auto-applying (Phase 8 / PR 8b on hold until all gates are GO and the owner says so);
tailored resume builder is planned, not built (see HANDOFF.md "Planned and not
built"). The "Add a job" page is built (PRs #33/#34).

## Metrics
Targets: precision@10 >= 80%, ~0 seniority pass-throughs.
UNVERIFIED: current measured values, because there is no verified human-labelled set
yet (G5 / DEC6 BLOCKED). Current eval figures are synthetic smoke tests only.

## Current state (2026-10-15)
Code state as of PR #34, 1111 tests passing, ruff/black/mypy clean, selfcheck --fast
21 PASS / 0 FAIL / 2 BLOCKED (G5, DEC6). Cumulative work covered by PRs #24 to #34
(base `8abeaa1`). See HANDOFF.md for detail.

## Owner-only (blocking completion)
Label 40 real JDs (15 traps, 10 apply) into `eval/labels.jsonl` + `label-check`;
rotate ALL keys (Groq, Gemini, Resend, Braintrust) and confirm; run mutmut under
WSL/Linux; authorize `eval --with-llm`; sign off `review_sample.md`; one complete
browser run with screenshots. The agent must not mark these PASS itself.
