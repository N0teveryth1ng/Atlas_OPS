# Atlas_OPS — Pre-Phase-8 Verification Report

- **Auditor role:** hostile auditor (not author).
- **Scope:** everything in `Atlas_OPS_preflight_prompt.md` §§0–13.
- **Branch audited:** `decision-engine` (base `main` @ `f4bb4a0`).
- **Self-check SHA:** `f4bb4a0` (see `selfcheck_report.json:git_sha`).
- **Toolchain:** Python 3.11.9, pytest 9.1.1, ruff 0.16.10, black 26.5.1, mypy 2.4.0.
  Models (config): extractor/evaluator `openai/gpt-oss-120b`, verifier
  `qwen/qwen3.8-27b`. Prompt versions: extractor `v1`, evaluator `v2`, verifier `v2`.
- **Primary evidence commands:**
  - `python -m atlas.cli selfcheck --full --report selfcheck_report.json`
  - `python -m atlas.cli eval`
  - `python -m pytest -q`
- **Safety:** no LLM spend, no live email, no applications were made. The eval was
  run in deterministic mode (no `--with-llm`).

---

## 1. Verdict

## **NO-GO for Phase 8.**

All defects fixable without owner input are fixed (D-1 through D-4, D-7) and the
machine-checkable quality gates now pass. Phase 8 stays blocked **only** on
owner-supplied inputs: the real human-labelled golden set (**G5/G3/G4/G6/G7
BLOCKED**), mutation testing being unavailable on native Windows (**G2/T5
BLOCKED**), and the owner's key-rotation/sign-off confirmations (**G1/G11**).

### Gate table

| Gate | Status | Evidence command | Notes |
|---|---|---|---|
| G1 | **FAIL** | `python -m atlas.cli selfcheck --full` | H1 FAIL only because this is a remediation branch (not `main`); H2–H7 PASS, H3 history scan PASS. Owner must confirm key rotation, then merge to `main` and re-run. |
| G2 | **PASS/BLOCKED** | `python -m atlas.cli selfcheck --full` | T1 PASS (517), T2 PASS (floors met), T3 PASS (ruff+black+mypy clean), T4 PASS (4 stable runs), T6 PASS, T7 PASS; T5 BLOCKED (mutmut needs WSL/Linux). |
| G3 | **BLOCKED** | `python -m atlas.cli selfcheck --full`; `python -m pytest tests/test_seniority_matrix.py` | Matrix 100% on 316 cases; 0 pass-throughs on the synthetic set. Real-set verification awaits G5. |
| G4 | **BLOCKED** | `python -m atlas.cli eval` | 100% year extraction, but on **synthetic** cases; no real set. |
| G5 | **BLOCKED** | `python -m atlas.cli selfcheck --fast` | `20 cases; count 20 < 40; missing provenance`. Owner must supply real labelled JDs. |
| G6 | **BLOCKED** | `python -m atlas.cli eval --with-llm` | Real-data precision@10 not measurable; live run not permitted (spend). |
| G7 | **BLOCKED** | `python -m atlas.cli eval` | Matcher beats baseline 100% vs 21.1% **on synthetic set**; real set missing. |
| G8 | **PASS** | `python -m atlas.cli selfcheck --fast` | A1/A2/A3 PASS; no pipeline path submits. |
| G9 | **PASS** | `python -m atlas.cli selfcheck --fast` | D1 idempotency PASS; D4 status-machine bypass test PASS; E2E dry run PASS; `sent ⊆ passed` hypothesis invariant PASS. |
| G10 | **PASS** | `python -m atlas.cli selfcheck --fast` | D3 evidence-substring validation PASS (present accepted, fabricated rejected); D4 state machine PASS. |
| G11 | **BLOCKED** | `review_sample.md` | Owner sign-off is a human action; not performed. |
| DEC1–DEC5 | **PASS** | `python -m atlas.cli selfcheck --fast` | Decision rule self-consistent (0 flips); seniority-year vetoes correct; `project_relevance`+`critic` cite verbatim evidence; counterfactuals monotonic (disqualifiers never promote); score monotonic under added red flags. |
| DEC6 | **BLOCKED** | `python -m atlas.cli selfcheck --fast` | Decision outcomes have no human-labelled ground truth yet; use the feedback loop (`atlas feedback`) to grow labelled decision cases. |

---

## 2. Defects found

### Fixed (one per commit, each with a regression test)

| ID | Severity | File | Reproduction | Fix |
|---|---|---|---|---|
| F-1 | **High** | `atlas/jd_parser.py` | Bare `"3 years"`/`"3 yrs"` with no "experience" keyword passed a fresher filter | `42781d3` (+316-case matrix) |
| F-2 | **High** | `atlas/sourcing.py` | Duplicate `run_sourcing` shadowed the real entry point | `a05572e` |
| F-3 | **High** | `skills.yaml` | `Spring`, `Spring Boot`, `.NET`, `.NET Core` did not match canonical skills | `bfbff69` (aliases 44→62) |
| F-4 | **Medium** | `auto_applications/*.py` | Hard-coded applicant name/phone/paths in legacy bots | `ec3e4e2` (env vars) |
| F-5 | **Medium** | `requirements.txt` | `tenacity`, `groq`, `pypdf`, `playwright`, `pytest>=8.0` unpinned | `f1e5636` |
| F-6 | **Low** | `atlas/config.py:15` | `vulture` unused import `Literal` | `4cf60ae` |
| F-7 | **Low** | `README.md` | `selfcheck` undocumented; duplicated roadmap lines; no `ATLAS_DB` | `7c7f41a` |
| D-1 | **High** | `filters.py`, `skills.py`, `ranker.py`, `feedback.py`, `llm.py` | Coverage below the 90% floor | `12357f5` (all floors now met) |
| D-2 | **High** | repo | ruff/black/mypy errors | `06090aa` + `72b28e2` + `9ecf7f2` (all three clean) |
| D-3 | **High** | `atlas/evaluator.py` | Fabricated `reasons_for` quote accepted | `51c2486` (`atlas/evidence.py`, retry + needs_review routing) |
| D-4 | **Medium** | `atlas/pipeline.py` | No runtime "filtered job never ships" invariant | `d0f5c13` (`atlas/job_status.py`, digest/emailer guards, hypothesis invariant) |
| D-7 | **Low** | `cli.py`, `profile_agent.py`, `companies.py`, `skills.py` | mypy typing gaps | `06090aa` |
| E-1 | **Low** | `atlas/selfcheck.py` | `git log -p` / black output crashed the subprocess reader on Windows (cp1252) | `cd9c2f7` (UTF-8 + `errors="replace"`, regression test) |

### Open / blocked (not fixable without owner input)

| ID | Severity | File:line | Reproduction | Expected vs actual |
|---|---|---|---|---|
| D-5 | **Medium** | — | `mutmut`/`cosmic-ray` on Win32 | Mutation score ≥80% (T5); tool unsupported on native Windows (needs WSL/Linux). |
| D-6 | **Critical** | `eval/golden_set.jsonl` | 20 synthetic cases, no `source`/`url`/`fetched_at`/`labeled_by` (E1/E2) | ≥40 real human-labelled, ≥15 traps (G5). Owner-supplied. |

---

## 3. Metrics (11.1)

Command: `python -m atlas.cli eval`.

### JD parse + hard filter
- Golden cases: **20** (synthetic).
- Year-extraction accuracy: **100.0% (19/19)** — 1 case carries no expected value.
- Experience pass-throughs: **0**.
- False rejects (apply→skip): **0**. Label inconsistencies: **0**.
- `PASS` (deterministic gate).

### Skill matching
- Cases: **19**; baseline (set overlap): **21.1% (4/19)**.
- Ontology matcher: **100.0% (19/19)** vs ≥95% target.
- `PASS`.

### Seniority matrix
- `tests/test_seniority_matrix.py`: **316 cases** (≥150 required), **100%** correct.

> **E5 caveat:** 100% figures are on **n≤19/20**. They are **not** evidence of
> real-world precision. Precision@10, seniority false-positive rate, recall,
> confusion matrix, and per-error breakdown against real data are **BLOCKED**
> by G5. No bootstrap CIs are meaningful at this sample size.

### Coverage (T2, `--cov --cov-branch`)
All floors (≥90%) met: `filters` 98%, `jd_parser` 99%, `skills` 100%,
`ranker` 100%, `digest` 92%, `emailer` 93%, `feedback` 96%, `llm` 98%,
`decision` 100%.

### Tests
- `517 passed`, 0 skipped, 0 xfail.
- T4 stable across 3 fixed-order runs + 1 random-order run.

### Lint / format / types (T3)
- `ruff check atlas tests` — clean.
- `black --check atlas tests` — clean.
- `mypy atlas` — clean (38 source files).
- `vulture atlas --min-confidence 80` — clean.

---

## 4. BLOCKED items — exactly what the owner must provide

1. **Real labelled golden set (G5/G3/G4/G6/G7, E1–E6):** ≥40 real JDs labelled
   `apply/skip` with a reason by you, including ≥15 trap cases, each with
   `source`, `url`, `fetched_at`, `labeled_by: "human"`. The agent must not and
   will not fabricate these.
2. **Key rotation confirmation (G1):** confirm `GROQ_API_KEY` and
   `RESEND_API_KEY` seen in history were revoked after the purge.
3. **Mutation testing (G2/T5):** run `mutmut` under WSL/Linux, or approve an
   alternative, for `filters.py`, `jd_parser.py`, `skills.py`, `ranker.py`.
4. **Live precision run (G6):** authorize a small `eval --with-llm` budget on the
   real set (the audit forbids unapproved spend).
5. **Manual spot check (G11):** review `review_sample.md` and sign off.

---

## 5. Known limitations (honest)

- Golden set is **synthetic (20)**; every percentage above is therefore a
  smoke-test figure, not accuracy.
- Verifier is a **different model but the same provider family** (Groq) — not
  fully independent (L6).
- `--fast` self-check replaces LLM calls with fixtures; `--full` does not make
  live LLM calls, so L3 determinism-on-live-LLM is unverified.
- Recall per source (C8) was **not** measured against the real set.
- Mutation coverage is unknown (T5 blocked).
- `review_sample.md` is a deterministic pre-screen, **not** an LLM
  recommendation sample.

## 6. Residual risks

- Seniority pass-throughs are the top risk; the fixed bare-years parser plus the
  316-case matrix reduce but do not eliminate it on unseen real JDs.
- Same-provider verifier can share blind spots with the evaluator.
- Evidence validation matches only whitespace/case-insensitively — a model can
  still quote a real-but-misleading substring; it cannot invent text.

---

## 7. Fixes still required before re-audit

1. D-6 (owner): supply the real golden set.
2. D-5 (owner/WSL): mutation score ≥80% under WSL/Linux.
3. G1 (owner): merge this branch to `main`, re-run `selfcheck --full` clean, and
   confirm key rotation.

Re-run `python -m atlas.cli selfcheck --full` after the above; Phase 8 remains
blocked until **all** gates are PASS.
