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
auto-applying (Phase 8 / PR 8b on hold until all gates are GO and the owner says so).
The tailored-resume generator, review UI and DOCX/PDF export are all built (PR #37
refined through #44, plus #45/#46 and the PR #47 review/export screen). The "Add a job"
page is built (PRs #33/#34). The
per-job result page and the owner's own Apply/Skip verdicts (PR #36) are human
bookkeeping only: verdicts append to the `owner_verdicts` table and never touch
`eval/labels.jsonl` or the decision.

## Metrics
Targets: precision@10 >= 80%, ~0 seniority pass-throughs.
UNVERIFIED: current measured values, because there is no verified human-labelled set
yet (G5 / DEC6 BLOCKED). Current eval figures are synthetic smoke tests only.

## Tailored resume
`atlas/resume.py` (PR #37, refined through #44, plus #45/#46 and the PR #47 UI)
drafts a resume tailored to one job from the approved profile: the model may only
rewrite the candidate's own material, every line must cite a profile **fact id** (a
sentence-level fact for prose), skills are built in code and alias-deduped (main group
capped at 14; hosting/observability tools held back unless the JD names them), invented
numbers/metrics/technologies are dropped, job keywords appear at most once, unmet
requirements are listed as missing and never added, and the layout passes an ATS lint
(no tables/images/text boxes/headers/footers). Empty/thin sections get a dedicated repair
call, then fall back to the section's own verbatim profile sentence (bounded by a 3-call
budget, with warnings); a repeat build is served from cache with zero LLM calls (keyed by
profile hash, job id, prompt version, generator version; a Regenerate action bypasses it).
Storage is the `tailored_resumes` table, versioned
per job and approved by the owner before export. The per-job page opens a side-by-side
review screen (base vs tailored, per-line fact ids / source badge / claim flags, Missing
requirements / Familiar skills / Additional tools / ATS lint), inline editing (each edit
saves a new `owner-edited` version and warns on a technology/number absent from the
profile) and approval; the export route returns **409** until a version is approved, and
only approved versions export to an ATS-friendly single-column
DOCX (`python-docx`) or PDF (`fpdf2`) with real selectable text and no tables/images/
text boxes/headers/footers, named `Name_Role_Company`. Exported text is folded to ASCII
at export time only, so fpdf2's core font never prints `?` for a non-breaking hyphen,
dash, curly quote or accent and the stored content is unchanged. It is grounded
generation only: it never submits an application.

## Current state (2026-10-18)
Code state as of PR #47, 1245 tests passing, ruff/black/mypy clean, selfcheck --fast
21 PASS / 0 FAIL / 2 BLOCKED (G5, DEC6). Cumulative work covered by PRs #24 to #47
(base `8abeaa1`). The tailored-resume generator, review UI, inline editing, approval and
DOCX/PDF export are all merged and working (a regression test covers PDF glyph folding).
What remains open is the Phase 8 auto-apply hold and the owner-only gates below. See
HANDOFF.md for detail.

## Owner-only (blocking completion)
Label 40 real JDs (15 traps, 10 apply) into `eval/labels.jsonl` + `label-check`;
rotate ALL keys (Groq, Gemini, Resend, Braintrust) and confirm; run mutmut under
WSL/Linux; authorize `eval --with-llm`; sign off `review_sample.md`; one complete
browser run with screenshots. The agent must not mark these PASS itself.
