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
