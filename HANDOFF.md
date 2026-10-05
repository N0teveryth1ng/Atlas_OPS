# HANDOFF.md

## State
- commit: d6e4a4fdb0f4 (main), origin/main in sync (everything through d6e4a4f pushed)
- tests: 539 passed
- selfcheck --fast: 21 PASS/0 FAIL/2 BLOCKED (G5, DEC6)
- selfcheck --full: 28 PASS/0 FAIL/3 BLOCKED (G5, DEC6, T5)

## Recent commits
d6e4a4f test(label-check): add cli_never_writes_labels test
61f50e9 test(label-check): restore tests and cover pool/duplicate/url rules
4a4be87 feat(label-check): validate pool membership, duplicates, and url match (read-only)
1671f6b feat(cli): add read-only atlas label-check

## Done
label-check read-only; 22 collected labeling tests; 539 tests pass; clean except blocked gates.

## Not done/hold
Phase 8/PR B on hold.

## Owner-only blocked
1. >=40 real JDs from eval/labeling_pool.jsonl into eval/labels.jsonl (>=15 traps, >=10 apply), run atlas label-check
2. confirm API-key rotation (Groq/Gemini/Resend)
3. mutmut under WSL/Linux (T5)
4. authorize live eval --with-llm (G6)
5. sign off review_sample.md
G1 stays BLOCKED until owner confirms.

## Limitations
RemoteOK+Remotive only; likely senior-heavy; golden set 20 synthetic; proves consistency, not real-world precision.

UNVERIFIED: block counts can change; verify with raw command output.

## Owner decisions
- Fresher, about 3 months internship; experience tolerance 1 year.
- Interface: CLI + scheduled run + email (FastAPI/Jinja UI dropped).
- auto_applications/ bots disabled from the pipeline, kept for Phase 8.
- History purge + key rotation chosen; rotation NOT yet confirmed (G1 stays BLOCKED).
- Sources: existing feeds + Adzuna + Greenhouse/Lever/Ashby; second Groq model as verifier.

## Also done (earlier phases)
Profile/input modes, query planner, collectors, normalize/dedupe, JD parser, hard filters, skill matching, evaluator + verifier + ranker, digest + Resend email + scheduler, feedback + tuning, selfcheck runner, seniority matrix, decision engine, evidence-quote validation, job status machine, label-check. Earlier commits: 21fb374 (audit), f4bb4a0 (remediation), 3e3197c (decision engine).

## More limitations
Adzuna has no keys; Greenhouse/Lever/Ashby were unreachable from the sandbox; verifier is the same provider family as the evaluator.

## How to run
python -m pytest -q
python -m atlas.cli selfcheck --fast
python -m atlas.cli selfcheck --full
python -m atlas.cli eval
python -m atlas.cli label-check

## Process lessons
Agent summaries were wrong several times (invented test counts, a commit holding only a temp file, placeholder output, edits reported but not saved). Verify every claim with raw command output in your own terminal.

## Next actions
1. Owner labels >=40 JDs from eval/labeling_pool.jsonl into eval/labels.jsonl, runs label-check.
2. Owner confirms API-key rotation.
3. Owner runs mutmut under WSL/Linux (T5).
4. Re-run selfcheck --full and the preflight.
5. Only if verdict is GO, plan PR B (apply layer).

## Owner decisions
- Fresher, about 3 months internship; experience tolerance 1 year.
- Interface: CLI + scheduled run + email (FastAPI/Jinja UI dropped).
- auto_applications/ bots disabled from the pipeline, kept for Phase 8.
- History purge + key rotation chosen; rotation NOT yet confirmed (G1 stays BLOCKED).
- Sources: existing feeds + Adzuna + Greenhouse/Lever/Ashby; second Groq model as verifier.

## Also done (earlier phases)
Profile/input modes, query planner, collectors, normalize/dedupe, JD parser, hard filters, skill matching, evaluator + verifier + ranker, digest + Resend email + scheduler, feedback + tuning, selfcheck runner, seniority matrix, decision engine, evidence-quote validation, job status machine, label-check. Earlier commits: 21fb374 (audit), f4bb4a0 (remediation), 3e3197c (decision engine).

## More limitations
Adzuna has no keys; Greenhouse/Lever/Ashby were unreachable from the sandbox; verifier is the same provider family as the evaluator.

## How to run
python -m pytest -q
python -m atlas.cli selfcheck --fast
python -m atlas.cli selfcheck --full
python -m atlas.cli eval
python -m atlas.cli label-check

## Process lessons
Agent summaries were wrong several times (invented test counts, a commit holding only a temp file, placeholder output, edits reported but not saved). Verify every claim with raw command output in your own terminal.

## Next actions
1. Owner labels >=40 JDs from eval/labeling_pool.jsonl into eval/labels.jsonl, runs label-check.
2. Owner confirms API-key rotation.
3. Owner runs mutmut under WSL/Linux (T5).
4. Re-run selfcheck --full and the preflight.
5. Only if verdict is GO, plan PR B (apply layer).
