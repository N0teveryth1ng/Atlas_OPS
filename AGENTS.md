# AGENTS.md

## Rules
- Use edit/write tool, never shell echo/heredocs.
- Windows/PS7 (no tail/grep/wc; use Select-Object -Last, Select-String).
- Paste raw output; never summarize/placeholders; report failures exactly; never write expected/minor/non-blocking unless output proves it.
- Never push without owner OK; no secrets, resume files, or eval/labeling_pool.jsonl.
- Never create/edit/relabel eval/labels.jsonl.
- No lowering thresholds, no deleting/skipping tests, no blanket noqa/type: ignore/pragma no cover.
- No Phase 8/PR B until owner says GO.
- One defect per commit + regression test.
- Gates before commit: pytest -q, ruff check atlas tests, black --check atlas tests, mypy atlas, selfcheck --fast.
- At session start read AGENTS.md, PRD.md, HANDOFF.md, then run git log --oneline -10 and git status -sb and confirm they match HANDOFF.md.
- Never create scratch or temp files in the repo.
- Never report a step as done without raw command output; after any edit, re-read the file to prove it saved.
- Never mark an owner-dependent gate (G1, G2/T5, G5, DEC6, G6, G11) PASS yourself.
- At session start read AGENTS.md, PRD.md, HANDOFF.md, then run git log --oneline -10 and git status -sb and confirm they match HANDOFF.md.
- Never create scratch or temp files in the repo.
- Never report a step as done without raw command output; after any edit, re-read the file to prove it saved.
- Never mark an owner-dependent gate (G1, G2/T5, G5, DEC6, G6, G11) PASS yourself.
