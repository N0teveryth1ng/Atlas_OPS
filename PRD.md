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
`/rejected`, owner-only `/add` with a `/add/progress` polling page, and a per-job
result page `/jobs/{job_id}`). Dashboard is password-guarded and refuses to bind
non-loopback without `DASHBOARD_PASSWORD`. Email delivery cancelled (local digest only).

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
in our own wrapper. Rate-limit headers are read from the raw HTTP response
(`with_raw_response`), since the SDK's parsed object strips them (PR #35).

## Non-goals
auto-applying (Phase 8 / PR 8b on hold until all gates are GO and the owner says so);
the tailored-resume **UI and export** are planned, not built (PR C2; the generator
itself landed in PR #37 with no UI). The "Add a job" page is built (PRs #33/#34). The
per-job result page and the owner's own Apply/Skip verdicts (PR #36) are human
bookkeeping only: verdicts append to the `owner_verdicts` table and never touch
`eval/labels.jsonl` or the decision.

## Metrics
Targets: precision@10 >= 80%, ~0 seniority pass-throughs.
UNVERIFIED: current measured values, because there is no verified human-labelled set
yet (G5 / DEC6 BLOCKED). Current eval figures are synthetic smoke tests only.

## Tailored resume
`atlas/resume.py` (PR #37) drafts a resume tailored to one job from the approved
profile: the model may only rewrite the candidate's own material, every line must
carry a verbatim profile quote, skills must already exist in the profile (alias-aware),
invented numbers/metrics/technologies are dropped, job keywords appear at most once,
unmet requirements are listed as missing and never added, and the layout passes an ATS
lint (no tables/images/text boxes/headers/footers). Storage is the `tailored_resumes`
table, versioned per job and approved by the owner before export (export is C2). It is
grounded generation only: it never submits an application.

## Current state (2026-10-16)
Code state as of PR #37, 1145 tests passing, ruff/black/mypy clean, selfcheck --fast
21 PASS / 0 FAIL / 2 BLOCKED (G5, DEC6). Cumulative work covered by PRs #24 to #37
(base `8abeaa1`). See HANDOFF.md for detail.

## Owner-only (blocking completion)
Label 40 real JDs (15 traps, 10 apply) into `eval/labels.jsonl` + `label-check`;
rotate ALL keys (Groq, Gemini, Resend, Braintrust) and confirm; run mutmut under
WSL/Linux; authorize `eval --with-llm`; sign off `review_sample.md`; one complete
browser run with screenshots. The agent must not mark these PASS itself.
