# HANDOFF.md

## State
- main in sync with origin/main; latest commits are docs-only handoff commits on top of d6e4a4f
- tests: 539 passed (verified: `python -m pytest -q` -> 539 passed in 18.45s)
- lint/type: `ruff check atlas tests` -> All checks passed; `black --check atlas tests` -> 63 files unchanged; `mypy atlas` -> no issues in 39 source files
- selfcheck --fast: 21 PASS / 0 FAIL / 2 BLOCKED (G5, DEC6)
- selfcheck --full: 28 PASS / 0 FAIL / 3 BLOCKED (G5, DEC6, T5)
- selfcheck exits 1 whenever any gate is BLOCKED; that is expected here, not a new failure.

## Recent commits
320b266 docs: complete handoff files (update rules/actions)
9ae42c8 docs: complete handoff files
5218bfd docs: add AGENTS.md, PRD.md, HANDOFF.md for session handoff
d6e4a4f test(label-check): add cli_never_writes_labels test
61f50e9 test(label-check): restore tests and cover pool/duplicate/url rules
4a4be87 feat(label-check): validate pool membership, duplicates, and url match (read-only)
1671f6b feat(cli): add read-only 'atlas label-check' for label validation
ebb9362 docs(preflight): record current state, block owner gates, keep raw JD pool local
3e3197c feat(decision): apply/review/skip decision engine, CLI, prompts, selfcheck (#11)

## Done
label-check read-only; 22 collected labeling tests; 539 tests pass; clean except blocked gates.

## Not done/hold
Phase 8/PR B on hold.

## Owner-only blocked
1. >=40 real JDs from eval/labeling_pool.jsonl into eval/labels.jsonl (>=15 traps, >=10 apply), run atlas label-check (G5/DEC6 inputs)
2. confirm API-key rotation (Groq/Gemini/Resend)
3. mutmut under WSL/Linux (T5)
4. authorize live eval --with-llm (G6)
5. sign off review_sample.md
G1 stays BLOCKED until owner confirms.

## Limitations
RemoteOK+Remotive only; likely senior-heavy; golden set 20 synthetic; proves consistency, not real-world precision.

Gate counts above were taken from a real run; re-run the commands in "How to run" before relying on them.

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
Entry point is `python -m atlas.cli`. Global flags: `-h/--help`, `--version`.

Commands and their flags (from `python -m atlas.cli --help` and per-command `--help`):
- `profile [--resume RESUME] [--describe DESCRIBE] [--describe-file DESCRIBE_FILE] [--out OUT] [--yes]`
- `review [--yes] [path]`
- `status`
- `run [--collect] [--limit LIMIT] [--no-email]`
- `decide [--limit LIMIT]`
- `explain job_id`
- `label-check [--labels LABELS] [--pool POOL]`
- `schedule [--hour HOUR] [--no-collect] [--limit LIMIT] [--no-email] [--no-immediate]`
- `collect`
- `eval [--golden GOLDEN] [--with-llm]`
- `selfcheck [--fast] [--full] [--report REPORT]`
- `feedback [--reason {too_senior,wrong_stack,bad_company,wrong_location,other,good_fit}] [--note NOTE] [--export] [job_id] [{good,bad}]`

Gates:
```
python -m pytest -q
ruff check atlas tests
black --check atlas tests
mypy atlas
python -m atlas.cli selfcheck --fast
python -m atlas.cli selfcheck --full
python -m atlas.cli eval
python -m atlas.cli label-check
```

## Process lessons
Agent summaries were wrong several times (invented test counts, a commit holding only a temp file, placeholder output, edits reported but not saved). Verify every claim with raw command output in your own terminal.

## Next actions
1. Owner labels >=40 JDs from eval/labeling_pool.jsonl into eval/labels.jsonl, runs label-check.
2. Owner confirms API-key rotation.
3. Owner runs mutmut under WSL/Linux (T5).
4. Re-run selfcheck --full and the preflight.
5. Only if verdict is GO, plan PR B (apply layer).

## Starting a new session
1. Read AGENTS.md, PRD.md, and this HANDOFF.md.
2. Run `git log --oneline -10` and `git status -sb`.
3. Report whether those two match what HANDOFF.md claims *before* changing anything.
4. If they disagree, say so and stop; do not assume HANDOFF.md is the correct one.