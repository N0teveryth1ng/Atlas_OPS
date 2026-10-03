# Atlas_OPS — Pre-Phase-8 Verification Report

- **Auditor role:** hostile auditor (not author).
- **Scope:** everything in `Atlas_OPS_preflight_prompt.md` §§0–13.
- **Branch audited:** `audit/preflight-phase8` (base `main` @ `65a6a61`).
- **Self-check SHA:** `73e039e` (see `selfcheck_report.json:git_sha`).
- **Toolchain:** Python 3.11.9, pytest 9.1.1. Models (config): extractor/evaluator
  `openai/gpt-oss-120b`, verifier `qwen/qwen3.8-27b`. Prompt versions: all `v1`.
- **Primary evidence commands:**
  - `python -m atlas.cli selfcheck --full --report selfcheck_report.json`
  - `python -m atlas.cli eval`
  - `python -m pytest -q`
- **Safety:** no LLM spend, no live email, no applications were made. The eval was
  run in deterministic mode (no `--with-llm`).

---

## 1. Verdict

## **NO-GO for Phase 8.**

Phase 8 stays blocked. Blocking reasons: golden set is still synthetic (**G5
BLOCKED**), mutation testing is unavailable on this platform (**G2/T5
BLOCKED**), coverage and lint/type floors fail (**G2**), evidence-quote
validation is not implemented (**G10**), and G1 needs the owner to confirm key
rotation and the work to land on `main`.

### Gate table

| Gate | Status | Evidence command | Notes |
|---|---|---|---|
| G1 | **FAIL** | `python -m atlas.cli selfcheck --full` | H1 FAIL (on audit branch, dirty); H2–H7 PASS; H3 history scan PASS. Owner must confirm key rotation. |
| G2 | **FAIL/BLOCKED** | `python -m atlas.cli selfcheck --full` | T1 PASS (443), T2 FAIL, T3 FAIL, T4 PASS, T5 BLOCKED (Win32), T6 PASS, T7 PASS. |
| G3 | **BLOCKED** | `python -m atlas.cli selfcheck --full`; `python -m pytest tests/test_seniority_matrix.py` | Matrix 100% on 316 cases; 0 pass-throughs only verifiable on a **real** golden set (missing). |
| G4 | **BLOCKED** | `python -m atlas.cli eval` | 100% year extraction, but on **synthetic** cases; no real set. |
| G5 | **BLOCKED** | `python -m atlas.cli selfcheck --fast` | `20 cases; count 20 < 40; missing provenance`. |
| G6 | **BLOCKED** | `python -m atlas.cli eval --with-llm` | Real-data precision@10 not measurable; live run not permitted (spend). |
| G7 | **BLOCKED** | `python -m atlas.cli eval` | Beats baseline 100% vs 21.1% **on synthetic set**; real set missing. |
| G8 | **PASS** | `python -m atlas.cli selfcheck --fast` | A1/A2/A3 PASS; no pipeline path submits. |
| G9 | **FAIL** | `python -m atlas.cli selfcheck --fast`; `pytest tests/test_pipeline.py` | D5 idempotency PASS; §11.2 combined failure-injection not fully demonstrated. |
| G10 | **FAIL** | static review | L4 evidence-substring validation absent; L8 invariant assertion absent. |
| G11 | **BLOCKED** | `review_sample.md` | Owner sign-off is a human action; not performed. |

---

## 2. Defects found

### Fixed (one per commit, each with a regression test)

| ID | Severity | File | Reproduction | Expected vs actual | Fix |
|---|---|---|---|---|---|
| F-1 | **High** | `atlas/jd_parser.py` | JD containing bare `"3 years"`/`"3 yrs"` with no explicit "experience" keyword | Should reject for a fresher; **passed** (silent seniority pass-through risk) | `42781d3` (+316-case matrix) |
| F-2 | **High** | `atlas/sourcing.py` | `mypy` `no-redef`; second `run_sourcing` shadowed the first | One defined entry point | `a05572e` |
| F-3 | **High** | `skills.yaml` | `Spring`, `Spring Boot`, `.NET`, `.NET Core` did not match their canonical skills | Should match (false negatives) | `bfbff69` (aliases 44→62) |
| F-4 | **Medium** | `auto_applications/*.py` | Hard-coded applicant name/phone/paths in legacy bots | PII must not be in tree | `ec3e4e2` (env vars) |
| F-5 | **Medium** | `requirements.txt` | `tenacity`, `groq`, `pypdf`, `playwright`, `pytest>=8.0` unpinned | Everything pinned | `f1e5636` |
| F-6 | **Low** | `atlas/config.py:15` | `vulture` unused import `Literal` | No dead code | `4cf60ae` |
| F-7 | **Low** | `README.md` | `selfcheck` undocumented; duplicated Phase 6/7 roadmap lines; no `ATLAS_DB` | Accurate README (H7) | `7c7f41a` |

### Open (not fixed)

| ID | Severity | File:line | Reproduction | Expected vs actual |
|---|---|---|---|---|
| D-1 | **High** | `atlas/filters.py`, `skills.py`, `ranker.py`, `feedback.py`, `llm.py` | `selfcheck --full` T2 | ≥90% coverage vs 69/81/62/79/62%. |
| D-2 | **High** | repo | `ruff check atlas tests` (57), `black --check atlas tests` (41 files), `mypy atlas` (20) | Zero errors. mypy clusters: `db.py` `int(cur.lastrowid)` ×7, `selfcheck.py` ×6, `profile_agent.py` ×2, `cli.py`, `companies.py`, `config.py`, `llm.py`, `skills.py`. |
| D-3 | **High** | `atlas/evaluator.py:76-84` | Mock LLM returns `Verdict` with a fabricated `reasons_for` quote | Quote must be a substring of the JD/profile; **no validation exists** (L4). |
| D-4 | **Medium** | `atlas/pipeline.py:98-205` | A job failing a hard filter returns `skip` at line 138-140, but there is no explicit runtime invariant/assertion (L8). | Add invariant + bypass test. |
| D-5 | **Medium** | — | `mutmut`/`cosmic-ray` on Win32 | Mutation score ≥80% (T5); tool unsupported on native Windows. |
| D-6 | **Critical** | `eval/golden_set.jsonl` | 20 synthetic cases, no `source`/`url`/`fetched_at`/`labeled_by` (E1/E2). | ≥40 real human-labelled, ≥15 traps (G5). |
| D-7 | **Low** | `atlas/cli.py`, `profile_agent.py`, `companies.py`, `skills.py` | `mypy` typing gaps (yaml stubs, `reconfigure` union-attr, extract return type). | Zero mypy errors (T3). |

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
`jd_parser` ~99%, `digest` 91.7%, `emailer` 91.1% (pass); **filters 69.1%,
skills 81.2%, ranker 62.5%, feedback 79.0%, llm 61.5%** (fail, floor 90%).

### Tests
- `443 passed`, 0 skipped, 0 xfail.
- T4 stable across 3 fixed-order runs + 1 random-order run.

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
- Coverage/lint/type debt (D-1/D-2) means unexercised filter/ranker paths may
  hide logic defects.
- Without L4 enforcement, a hallucinated evidence quote could justify a bad
  `apply`.
- Same-provider verifier can share blind spots with the evaluator.

---

## 7. Fixes still required before re-audit

1. D-6 (owner): supply the real golden set.
2. D-1/D-2: raise coverage to ≥90% on the five modules; zero ruff/black/mypy.
3. D-3: enforce `Verdict` evidence is a substring of JD/profile + regression test.
4. D-4: add the "filtered job never ships" pipeline invariant + bypass test.
5. D-5: mutation score ≥80% under WSL/Linux.
6. G1: merge to `main`, re-run selfcheck clean, confirm key rotation.

Re-run `python -m atlas.cli selfcheck --full` after the above; Phase 8 remains
blocked until **all** gates are PASS.
