# HANDOFF.md

## State
- main is AHEAD of origin/main (local commits only, not pushed): web dashboard work on top of d6a06a1
- tests: 951 passed (verified: `python -m pytest -q` -> 951 passed in 17.53s)
- lint/type: `ruff check atlas tests` -> All checks passed; `black --check atlas tests` -> 90 files unchanged; `mypy atlas` -> no issues in 49 source files
- selfcheck --fast: 21 PASS / 0 FAIL / 2 BLOCKED (G5, DEC6)
- selfcheck --full and T5: not re-run this session
- selfcheck exits 1 whenever any gate is BLOCKED; that is expected here, not a new failure.

## Recent commits
cabe0c6 fix(dashboard): show only decided APPLY/REVIEW rows, flag unknown seniority
814f4c9 test: isolate dashboard env so DASHBOARD_PASSWORD cannot leak between tests
55d6b99 docs(handoff): record groq model verification and session status
651426f docs: web dashboard flow, password guard, and handoff update
4f0b8ba chore: gitignore personal resume.pdf
e13b3e3 chore(dashboard): lint/format fixes and document DASHBOARD_PASSWORD
528d8c8 test(dashboard): cover upload limits, auth, lock, filtering
91b7326 feat(dashboard): web UI for setup, profile approval, find jobs, results
c905591 STEP6a: add web UI tests
aa2557d STEP5b: pass host to create_app and enforce password check
8c4a91b fix: sqlite3.Row access without .get()
e318b46 fix: preserve index behavior when decision missing
08b925e feat: add MIT license and document BrainTrace observability
80ea58f test: build path fixtures at runtime so selfcheck A4 stays green
edda227 feat: add optional Braintrust observability with PII redaction
4a883ef Update README.md (#13)
bca0f70 docs: add pipeline architecture diagram to README (#12)
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
Web dashboard (FastAPI, no Jinja): /setup (PDF <=5MB + description), /profile
(review/edit/approve, pipeline blocked until approved), Find jobs (background
thread, one-run lock), /status + /api/status, results table (only APPLY/REVIEW
decisions; SKIP hidden; no-decision rows listed under "Not yet evaluated"; a
stored APPLY whose seniority is unknown is shown as REVIEW with a visible
warning; source=manual rows hidden) with APLD/NTAPLD/PEND buttons, /rejected
with filter rule/reason, /health probe. /profile approval now requires at least
one location or "anywhere". DASHBOARD_PASSWORD login (constant-time compare,
same-origin CSRF, HTML escaping); refuses to bind non-loopback without the
password. db.py shippable_jobs/rejected_jobs added; CandidateProfile.locations
added. Procfile now runs the guarded CLI entrypoint. 951 tests pass;
ruff/black/mypy pass; selfcheck fast 21 PASS / 0 FAIL / 2 BLOCKED.
Dashboard tests are env-isolated (tests/conftest.py clears DASHBOARD_PASSWORD /
ATLAS_DB) so order does not matter.

From earlier: label-check read-only; 22 collected labeling tests.
Braintrust (BrainTrace) observability: MIT license, PII-redacted LLM tracing via
atlas/observability.py, and eval metrics published as named scores
(year_extraction_accuracy, skill_matching_accuracy, llm_precision_at_k) into
Braintrust project atlasops (3c5416f9-ff13-4c16-9a07-71d0e5a8c090).

## Not done this session
- Groq models API call is blocked from the sandbox (Cloudflare `error code: 1010`);
  model ids were cross-checked against Groq's published catalogue instead
  (`openai/gpt-oss-120b` and `qwen/qwen3.8-27b` both live on Groq; verifier is a
  different family from the evaluator). Re-run the /models check on a real
  network before relying on it.
- "claims not verified with a command" final list: see the end of the session
  transcript that produced commit 651426f.

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
- Interface: CLI + scheduled run (email delivery cancelled) PLUS the web dashboard (/setup -> /profile -> find -> results) added this session.
- auto_applications/ bots disabled from the pipeline, kept for Phase 8.
- History purge + key rotation chosen; rotation NOT yet confirmed (G1 stays BLOCKED).
- Sources: existing feeds + Adzuna + Greenhouse/Lever/Ashby; second Groq model as verifier.

## Also done (earlier phases)
Profile/input modes, query planner, collectors, normalize/dedupe, JD parser, hard filters, skill matching, evaluator + verifier + ranker, digest + Resend email + scheduler, feedback + tuning, selfcheck runner, seniority matrix, decision engine, evidence-quote validation, job status machine, label-check. Earlier commits: 21fb374 (audit), f4bb4a0 (remediation), 3e3197c (decision engine).

## More limitations
Adzuna has no keys; Greenhouse/Lever/Ashby were unreachable from the sandbox; verifier is the same provider family as the evaluator.

## How to run
Entry point is `python -m atlas.cli`. Global flags: `-h/--help`, `--version`.
Verified against `python -m atlas.cli --help` on 2026-10-05; all 12 commands below match the real parser.

Commands and their flags (from `python -m atlas.cli --help` and per-command `--help`):
- `profile [--resume RESUME] [--describe DESCRIBE] [--describe-file DESCRIBE_FILE] [--out OUT] [--yes]`
- `review [--yes] [path]`
- `status`
- `run [--collect] [--limit LIMIT]`
- `decide [--limit LIMIT]`
- `explain job_id`
- `label-check [--labels LABELS] [--pool POOL]`
- `schedule [--hour HOUR] [--no-collect] [--limit LIMIT] [--no-immediate]`
- `collect`
- `eval [--golden GOLDEN] [--with-llm]`
- `selfcheck [--fast] [--full] [--report REPORT]`
- `feedback [--reason {too_senior,wrong_stack,bad_company,wrong_location,other,good_fit}] [--note NOTE] [--export] [job_id] [{good,bad}]`
- `dashboard [--host HOST] [--port PORT]` (also `uvicorn app:app` for local; Procfile guards non-loopback with DASHBOARD_PASSWORD)

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