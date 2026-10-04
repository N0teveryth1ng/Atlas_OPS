# Atlas_OPS — Pre-Phase-8 Verification Report

- **Auditor role:** hostile auditor (not author).
- **Scope:** everything in `Atlas_OPS_preflight_prompt.md` §§0–13.
- **Branch audited:** `main` @ `3e3197c` (post PR #11, decision engine).
- **Self-check SHA:** `3e3197c` (see `selfcheck_report.json:git_sha`).
- **Toolchain:** Python 3.11.9, pytest 9.1.1, ruff 0.16.10, black 26.5.1, mypy 2.4.0.
  Models (config): extractor/evaluator `openai/gpt-oss-120b`, verifier
  `qwen/qwen3.8-27b`. Prompt versions: extractor `v1`, evaluator `v2`, verifier `v2`,
  project_relevance `v1`, decision_critic `v1`.
- **Primary evidence commands:**
  - `python -m atlas.cli selfcheck --full --report selfcheck_report.json`
  - `python -m atlas.cli eval`
  - `python -m pytest -q`
- **Safety:** no LLM spend, no live email, no applications were made. The eval was
  run in deterministic mode (no `--with-llm`).

---

## 1. Verdict

## **NO-GO for Phase 8.**

All defects fixable without owner input are fixed (D-1 through D-4, D-7), the
machine-checkable quality gates pass on a **clean `main` @ `3e3197c`**
(self-check: 28 PASS / **0 FAIL** / 3 BLOCKED — G5, DEC6, T5), and the **Decision
Engine** (PR #11) is merged. Phase 8 stays blocked **only** on owner-supplied
inputs: the real human-labelled golden set (**G5/G3/G4/G6/G7 and DEC6 BLOCKED** — a
raw, scoring-free 70-JD labeling pool is staged at `eval/labeling_pool.jsonl`),
mutation testing being unavailable on native Windows (**G2/T5 BLOCKED**), and the
owner's **personal** confirmations (**G1** key rotation, **G11** spot-check
sign-off) — both **BLOCKED** until the owner acts. No owner-dependent gate is
marked PASS here on the agent's own authority.

### Gate table

| Gate | Status | Evidence command | Notes |
|---|---|---|---|
| G1 | **BLOCKED** | `python -m atlas.cli selfcheck --full` | Hygiene H1–H7 PASS on clean `main` (H1 clean/current, H3 history scan PASS), but the **gate** stays BLOCKED until the **owner personally confirms** the `GROQ_API_KEY`/`RESEND_API_KEY` seen in history were revoked after the purge (§4). The agent must not mark this PASS on its own. |
| G2 | **BLOCKED** | `python -m atlas.cli selfcheck --full` | T1 PASS (517), T2 PASS (floors met), T3 PASS (ruff+black+mypy clean), T4 PASS (4 stable runs), T6 PASS, T7 PASS; T5 BLOCKED (mutmut needs WSL/Linux — owner environment). Gate remains BLOCKED until T5 runs. |
| G3 | **BLOCKED** | `python -m atlas.cli selfcheck --full`; `python -m pytest tests/test_seniority_matrix.py` | Matrix 100% on 316 cases; 0 pass-throughs on the synthetic set. Real-set verification awaits G5. |
| G4 | **BLOCKED** | `python -m atlas.cli eval` | 100% year extraction, but on **synthetic** cases; no real set. |
| G5 | **BLOCKED** | `python -m atlas.cli selfcheck --fast` | Golden set still 20 synthetic cases (count < 40, no provenance). A raw, scoring-free **70-JD** pool is staged at `eval/labeling_pool.jsonl`; owner must label it (`apply`/`skip` + reason + `source`/`url`/`fetched_at`/`labeled_by`). |
| G6 | **BLOCKED** | `python -m atlas.cli eval --with-llm` | Real-data precision@10 not measurable; live run not permitted (spend). |
| G7 | **BLOCKED** | `python -m atlas.cli eval` | Matcher beats baseline 100% vs 21.1% **on synthetic set**; real set missing. |
| G8 | **PASS** | `python -m atlas.cli selfcheck --fast` | A1/A2/A3 PASS; no pipeline path submits. |
| G9 | **PASS** | `python -m atlas.cli selfcheck --fast` | D1 idempotency PASS; D4 status-machine bypass test PASS; E2E dry run PASS; `sent ⊆ passed` hypothesis invariant PASS. |
| G10 | **PASS** | `python -m atlas.cli selfcheck --fast` | D3 evidence-substring validation PASS (present accepted, fabricated rejected); D4 state machine PASS. |
| G11 | **BLOCKED** | `review_sample.md` | Owner sign-off is a human action; not performed. |
| DEC1–DEC5 | **PASS** | `python -m atlas.cli selfcheck --fast` | Decision rule self-consistent (0 flips); seniority-year vetoes correct; `project_relevance`+`critic` cite verbatim evidence; counterfactuals monotonic (disqualifiers never promote); score monotonic under added red flags. |
| DEC6 | **BLOCKED** | `python -m atlas.cli selfcheck --fast` | Decision outcomes have no human-labelled ground truth yet. Label the staged `eval/labeling_pool.jsonl` and/or grow labels via `atlas feedback`; DEC6 unblocks once labelled `apply`/`review`/`skip` decisions exist. |

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

A raw, **scoring-free** labeling pool is staged at `eval/labeling_pool.jsonl`:
**70 current JDs** (54 RemoteOK, 16 Remotive; posted `2026-08-04`..`2026-10-02`),
fields `pool_id, source, source_id, title, company, location, remote_type, url,
urls, posted_at, fetched_at, description`. It deliberately contains **no fit
scores and no model output**, so labeling is not anchored.  *(The file is
currently untracked — commit it or add it to `.gitignore` when you decide how to
handle it.)*

1. **Label the pool / grow the real golden set (G5/G3/G4/G7, E1–E6, DEC6):**
   copy labelled cases into `eval/golden_set.jsonl` with `label: apply|skip`, a
   `reason`, and `source`/`url`/`fetched_at`/`labeled_by: "human"`, until ≥40 real
   cases (including ≥15 traps — jobs that *look* junior but need more experience
   or the wrong stack). For DEC6 also record the decision you would give
   (`apply`/`review`/`skip`) so Decision-Engine outcomes have human ground truth.
   The agent must not and will not fabricate these.
2. **Key rotation confirmation (G1):** confirm the `GROQ_API_KEY` and
   `RESEND_API_KEY` seen in history were revoked after the purge (the H3 history
   scan already PASSes; this is your sign-off).
3. **Mutation testing (G2/T5):** run `mutmut` under WSL/Linux for `filters.py`,
   `jd_parser.py`, `skills.py`, `ranker.py`, or approve an alternative. It cannot
   run on native Windows.
4. **Live precision run (G6):** authorize a small `eval --with-llm` budget on the
   real set (the audit forbids unapproved spend; roughly one run over N cases).
5. **Manual spot check (G11):** review `review_sample.md` and sign off.

---

## 5. Known limitations (honest)

- Golden set is **synthetic (20)** and the staged 70-JD pool is **unlabelled**;
  every percentage above is a smoke-test figure, not accuracy.
- **Source coverage gap:** the staged pool is **RemoteOK + Remotive only** — the
  Greenhouse/Lever/Ashby ATS boards were unreachable from the sandbox and no
  Adzuna keys are configured — so a golden set labelled from it will **not cover
  the Greenhouse/Lever/Ashby/Adzuna sources the owner actually uses**. The pool is
  also likely **senior-heavy**, so fresher-friendly "apply" cases may be scarce.
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

1. D-6 (owner): label the staged `eval/labeling_pool.jsonl` into the golden set
   (≥40 real cases, ≥15 traps, full provenance) and add decision-outcome labels
   (DEC6).
2. D-5 (owner/WSL): mutation score ≥80% under WSL/Linux.
3. G6 (owner): authorize the live `eval --with-llm` precision run once the real
   set exists.
4. G11 (owner): review and sign off `review_sample.md`.
5. G1 (owner): confirm key rotation.

Re-run `python -m atlas.cli selfcheck --full` after the above; Phase 8 remains
blocked until **all** gates are PASS. As of this revision the clean `main`
self-check is **28 PASS / 0 FAIL / 3 BLOCKED (G5, DEC6, T5)**.
