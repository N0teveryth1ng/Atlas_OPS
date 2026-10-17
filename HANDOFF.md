# HANDOFF.md

## State (verified 2026-10-17)

- Code state as of PR #44, in sync with `origin/main`.
- This state is the cumulative result of PRs **#24 to #44** (base `8abeaa1`).
- Tests: **1208 passed** (`python -m pytest -q`).
- Lint / format / types: `ruff check atlas tests` clean; `black --check atlas tests`
  clean (line length 100); `mypy atlas` clean.
- Eval: **unchanged baseline** (`python -m atlas.cli eval`), last 12 lines:

```
False rejects (apply->skip): []
Label inconsistencies:       []
RESULT:                      PASS
Braintrust: published year_extraction_accuracy -> https://www.braintrust.dev/app/My%20Project

== Skill matching ==
Skill cases:                 19
Baseline (set-overlap):      21.1% (4/19)
Ontology matcher:            100.0% (19/19) target >= 95%
Matcher failures:            []
RESULT:                      PASS
Braintrust: published skill_matching_accuracy -> https://www.braintrust.dev/app/My%20Project
```

- `selfcheck --fast`: **21 PASS / 0 FAIL / 2 BLOCKED (G5, DEC6)**. It exits 1 whenever
  any gate is BLOCKED; that is expected here, not a new failure.
- `selfcheck --full` not re-run this session.

## Merged PRs #24 to #44

| PR | commit | merged (UTC) | branch | title |
|---|---|---|---|---|
| #24 | `8abeaa1` | 2026-10-13T02:33:58Z | pr1/run-recovery-and-progress | fix(runs): fail abandoned runs and persist live run progress |
| #25 | `b2cebd1` | 2026-10-13T03:45:20Z | taskb/cheap-prefilter-and-run-budgets | Task B: cheap deterministic pre-filter + per-run cost caps |
| #26 | `585cb17` | 2026-10-13T04:20:51Z | fix/flaky-collection-timeout-test | fix(sourcing): make whole-collection timeout deterministic |
| #27 | `b3139e6` | 2026-10-13T13:58:09Z | taskc/reapply-filters-on-profile-change | feat: re-apply hard filters on profile approval and every run start |
| #28 | `795f010` | 2026-10-14T04:57:08Z | taskd/groq-pacing-logging-status | perf: Groq retry logging + fix source kept counts |
| #29 | `7a73982` | 2026-10-14T07:44:27Z | taskd/followup-status-pacing | feat(status): live job/step/ETA on /status + Groq token-budget pacing |
| #30 | `f16a05c` | 2026-10-15T05:24:31Z | taskd/daily-limit-and-token-cap | feat(budget): pause on Groq daily limit and cap per-run tokens |
| #31 | `11a408b` | 2026-10-15T06:54:09Z | taskd/find-daily-limit-guard | feat(dashboard): refuse Find jobs when Groq daily limit is near or paused |
| #32 | `a15c1a1` | 2026-10-15T10:45:52Z | docs/handoff-complete | docs(handoff): refresh HANDOFF/PRD to current state; add Task D evidence, planned-not-built, owner-only |
| #33 | `eead966` | 2026-10-15T11:41:53Z | taske/add-a-job | feat(dashboard): owner-only Add a job page running the same pipeline |
| #34 | `5b0d6ea` | 2026-10-15T17:21:21Z | task/add-background-progress | feat(dashboard): run Add a job in the background with a progress page |
| #35 | `224b376` | 2026-10-16T03:28:18Z | prA/transient-retries-and-live-report | fix(llm): read rate-limit headers from the raw response; cover transient retries |
| #36 | `cac9b21` | 2026-10-16T03:55:07Z | prB/results-page-improvements | feat(dashboard): per-job result page with owner verdicts |
| #37 | `a3ee389` | 2026-10-16T04:39:03Z | prC1/tailored-resume-generator | feat(resume): grounded tailored-resume generator (no UI) |
| #38 | `bba204c` | 2026-10-16T09:30:13Z | prC/profile-editor | feat(profile): rich profile editor (internships/work/projects/education/links/certs) |
| #39 | `c6a6276` | 2026-10-17T04:11:02Z | prC1b/resume-fact-grounding | fix(resume): ground tailored-resume lines by profile fact ids; build skills in code |
| #40 | `dc648aa` | 2026-10-17T04:57:22Z | prC1c/grounding-claim-strength | fix(resume): reject unsupported strength claims; ground only tech/number; ontology coverage; evidence-linked skills |
| #41 | `e67a847` | 2026-10-17T05:27:38Z | prC1c/coverage-fact-choice | fix(resume): cite the tech fact that names the covered concept, not the entry title |
| #42 | `462d198` | 2026-10-17T05:52:44Z | prC1e/audit-claim-coverage | fix(resume): stricter claim/coverage grounding; evidence concept skills from prose |
| #43 | `6cb8caa` | 2026-10-17T06:10:11Z | prC1f/data-structure-nouns | fix(resume): cover 'data structures' only when a fact names a noun form |
| #44 | `d5fce39` | 2026-10-17T07:36:13Z | prC1d/lexical-grounding-repair | fix(resume): ground bullets by sentence facts, repair thin sections, clean tech |

Earlier context: #20 docs (owner merge workflow), #21 Himalayas collector, #22 kit claim
kind, #23 non-job-listing filter.

## What the app does now

- Interface: CLI (`python -m atlas.cli`) + scheduler + local web dashboard. Email
  delivery is cancelled; the digest is local.
- Dashboard flow: `/setup` (resume PDF <= 5MB + description) -> `/profile`
  (review / edit / approve; since PR #38 also edits internships, work, projects,
  education, links and certifications; approving re-filters; pipeline blocked until
  approved) -> **Find jobs**
  (background thread, one-run lock) -> `/status` + `/api/status` -> results table
  (only APPLY/REVIEW decisions; SKIP hidden; undecided rows under "Not yet evaluated";
  stored APPLY with unknown seniority shown as REVIEW with a warning; source=manual
  hidden) with APLD / NTAPLD / PEND bookkeeping -> **per-job result page**
  `GET /jobs/{job_id}` (PR #36; each results row links here) showing the score line
  `N / 100 (REVIEW starts at 65)`, confidence, seniority assessment, missing skills,
  the score breakdown by dimension (score / weight / spread), vetoes (rule + reason +
  quote + source), and deduped reasons (each quote once, split into "Cited on both
  sides" / reasons for / reasons against / "Evidence"), plus **Prepare application**
  (opens `/jobs/{id}/kit` for any decision, including SKIP) and a **Your verdict**
  form -> `/rejected` with filter rule + reason -> `/health`. **Add a job** (`/add`,
  owner-only; PR #33) pastes a JD and runs the same pipeline; since PR #34 it
  evaluates in a background thread and redirects (303) to `/add/progress`, which polls
  `/api/add-status` (current step, Groq rate-limit wait, LLM calls / tokens) and
  renders the decision when done.
  `DASHBOARD_PASSWORD` login (constant-time compare, same-origin CSRF, HTML
  escaping); refuses to bind non-loopback without the password.
- Pipeline: profile -> query planner -> collectors -> normalize/dedupe (SQLite) -> JD
  parser (regex + LLM) -> hard filters (code) -> skill match -> evaluator (LLM) ->
  adversarial verifier -> ranker -> decision engine (APPLY/REVIEW/SKIP) -> digest +
  dashboard -> feedback.
- Cost controls: deterministic pre-filter rejects failures before any LLM call; per-run
  LLM call + token caps; hard filters re-applied on profile approval and every run
  start; abandoned runs are failed and live progress persists (run recovery).

## Owner verdicts (PR #36)

- The per-job page lets the owner record their own Apply / Skip call with a one-line
  reason (`POST /jobs/{job_id}/verdict`). Each submission is **appended** to the
  `owner_verdicts` table (`job_id`, `verdict`, `reason`, `system_decision`, `created_at`)
  and the page shows the latest; `system_decision` snapshots what the engine said at
  that moment so a later engine change cannot rewrite history.
- This is human bookkeeping only: it never writes to `eval/labels.jsonl` and never
  changes filters, ranking, decision or prompts.
- **Prepare application** opens `/jobs/{id}/kit` for any stored decision (APPLY,
  REVIEW, SKIP); the kit gate was widened from APPLY/REVIEW to all three.

## Tailored resume generator (PRs #37, #39, #40, #41, #42, #43, #44, + the empty-section/skills-cache fix)

- `atlas/resume.py` + `prompts/tailored_resume.md` (version 4) turn the approved
  profile and one parsed job into a `TailoredResume`. **No UI yet** (PR C2 adds the
  job-page button, review page, approval and DOCX/PDF export); the generator is code
  only and nothing runs it automatically.
- Grounding is by profile **fact ids**, not free-text quotes. `build_facts` numbers the
  profile into citable facts and splits every prose field into **sentence** facts
  (`[P1.summary.s1]`, `[P1.outcomes.s2]`, `[I1.s2]`, `[W1.s1]`, `[W1.tech]`,
  `[ED1.line]`, `[CERT]`, `[LINK]`), so a bullet cites the exact sentence it rests on and
  cannot borrow unrelated detail (#44); `render_profile_facts` shows them to the model.
  Every summary sentence and bullet must cite at least one real id, and code maps each
  id back to its verbatim text.
- The model may only rewrite the candidate's own material; code, not the prompt,
  enforces the hard rules:
  - a line is dropped when it is third-person/meta, when it carries hidden characters,
    when a bullet does not start with an action verb (a code-supplied
    `source="profile-verbatim"` fallback is exempt), when it introduces a technology,
    number or mid-sentence proper noun its cited facts lack, or when any JD keyword
    would exceed the global cap. Only technologies (skill ontology + proper nouns) and
    numbers need grounding; generic nouns (data, design, system, API/APIs, AI, service,
    platform, pipeline) are ignored, with `AI -> LLM` and `API -> REST` (#40);
  - a line is dropped when it claims ownership/leadership/reliability/scale or a
    strength adjective (`led`, `owning`, `architected`, `zero-downtime`, `robust`,
    `reliable`, `production-grade`, `from scratch`, `core`, `enterprise`, `scalable`,
    `end-to-end`, `delivered`, ...) with a word the cited facts do not contain — a
    "Sole engineer" title does not license "led" or "owning" (#40, #41);
  - skills are **built in code, never by the model**, and are evidence-linked: a skill
    is main-eligible only when a project/internship/work tech list, a project
    summary/outcomes, or an internship/work description names it (or the owner set
    `confirmed`), otherwise it moves to "Familiar with (confirm before use)"; the main
    group is capped at **14**, JD-matching skills come first, then the rest by how many
    facts cite them, aliases collapse to one name (`JWT`/`JWT Auth`,
    `PostgreSQL`/`PostgreSQL (Neon)`/`Neon`, `GitHub Actions`/`CI/CD (GitHub Actions)`/
    `Git/GitHub`), and hosting/observability tools (`Render`, `Vercel`, `Braintrust`,
    `Neon`) are held back unless the JD names them; the cap overflow and those tools go
    to a separate `additional_tools` list shown on the review page and **not exported
    unless selected**. `TailoredResume.skill_facts` records the fact id that evidences
    each main skill (#40, #41);
  - requirements are classified by ontology/alias/family first (`REST API Design`
    covers `APIs`, `Idempotency`/`Circuit Breakers` cover design patterns,
    `PostgreSQL`/`Redis` cover storage, familiar `AWS`/`GCP` give `partial` cloud); a
    requirement that has a concept family is judged *only* by that family and the
    model's judgement can never upgrade it, so `data structures` stays missing unless a
    fact names a data structure or algorithm **as a noun** — a bare plural verb
    ("queues high-volume events"), a tech-stack phrase ("full stack") or a hyphenated
    `full-stack` does not count, but "a priority queue", "message queues" and "binary
    search" do (#42, #43); the 8-character verbatim-quote rule applies only to free-text
    claims, not skill/tech names, and the cited fact is the entry's `.tech` fact whose
    text names the concept (#40, #41);
  - the second summary line is kept only when it overlaps **less than 60%** (Jaccard over
    content words) with every bullet; otherwise it is dropped, so the summary never
    just repeats a bullet (the first line is always built in code);
  - such requirements the profile lacks go to `missing_requirements` and are never added.
- Empty/thin sections are never left blank and never padded. After validation, every
  project left with **no** bullets gets **one repair call of its own** (naming the
  project, its sentence ids and its earlier drop reasons, up to 2 attempts); the
  internship section likewise while it has fewer than 3 grounded bullets. Every LLM call
  — the draft retries and these per-section repairs — draws from a single
  **3-call-per-resume** budget. Anything still empty/thin falls back **in code** to that
  section's own sentence(s) copied **verbatim** from the profile, marked
  `source="profile-verbatim"` (exempt from the action-verb rule), and a warning is
  emitted for every empty/thin section and every verbatim fallback used.
- If the profile lacks internship/work/project/link detail the generator **refuses**
  (before any LLM call) and lists exactly what to add on the Profile page; it never pads.
- Header, experience role/company/duration, project titles/tech, education and
  certifications are copied verbatim from the profile, so dates and employers cannot
  change. Dates are additionally rendered in **proper case** at output only
  (`"2026-03-23 - 2026-06-30"` -> `"March 23, 2026 - June 30, 2026"`); the stored fact
  keeps the owner's original string.
- `ats_lint` checks the flattened blocks (`resume_blocks`): it fails on any table,
  image, text box, icon or header/footer content, on hidden characters, on empty text
  and on a missing standard heading (Summary/Skills/Experience/Projects/Education).
- Storage: `tailored_resumes` table (`job_id`, `version`, `content_json`,
  `prompt_version`, `model`, `profile_hash`, `created_at`, `approved_at`);
  `save_tailored_resume` versions per job, `latest_tailored_resume`,
  `find_cached_tailored_resume`, `approve_tailored_resume` (idempotent),
  `approved_tailored_resume`. Only approved versions are meant to be exported (C2).
- Determinism and cost: every resume LLM call uses `temperature 0`, and
  `generate_tailored_resume_cached` caches by `(profile hash, job id, prompt version)`
  so a repeat build returns the identical resume with **zero** LLM calls. Generation is
  **on demand only** (never in a bulk run) and reports the tokens and calls it spent.
- `generate_tailored_resume_under_caps` wraps the client in `_BudgetedClient` (per-run
  call/token caps), records tokens via `add_llm_tokens`, and lets
  `GroqDailyLimitReached` propagate. Profile/resume text never reaches Braintrust
  (prompt fingerprints only; covered by a test).

## Profile editor (PR #38)

- `/profile` now edits the richer facts the tailored-resume generator needs, all stored
  in the approved profile JSON (`atlas/schemas.py`, `profiles.data_json`):
  - internships and work: role, company, months, free-text dates, tech, description
    lines (one bullet per line);
  - projects: title, summary, outcomes, tech;
  - education: degree, field, institution, graduation year with an `expected` flag;
  - links: github / linkedin / portfolio (kept only when absolute `http`/`https`);
  - certifications; skills may carry an optional third token
    `name:proficiency:evidenced-by` and an optional fourth `:confirm` token to list a
    skill in the main Skills group without tech-list evidence (PR #40).
- Add and remove entries. Validation: any internship, work or project entry missing a
  description/outcome blocks approval with a warning naming the entry (nothing is
  invented or auto-filled); non-`http`/`https` links are rejected. Everything is
  HTML-escaped. Any change requires re-approval through the existing flow, which
  re-filters (`reapply_filters`, reason `profile approved`).
- The editor never calls the LLM and never sends profile text to Braintrust (covered by
  a test). No changes to filter, decision, ranking or prompt logic.

## Groq budget controls (PRs #28-#31, #34)

- Free tier (chat models): ~30 requests/min and ~1000 requests/day; token windows
  ~8K tokens/min and ~200K tokens/day. 429 responses carry `Retry-After` and
  `x-ratelimit-*` headers. Roughly 6-9K tokens per fully processed job.
- Caps in `config.yaml` / `RunConfig`:
  `max_jobs_per_run: 12`, `max_llm_calls_per_run: 100`,
  `max_llm_tokens_per_run: 120000`, `max_llm_tokens_per_day: 200000`.
- All transport retrying happens in our own wrapper (PR #34): the Groq SDK is
  built with `max_retries=0`, so its internal 2-retry backoff can no longer bypass
  our pacing, wait callbacks or daily-limit detection. Each retry logs
  `status`, `Retry-After`, `model`, `call_type` and the remaining token header.
- Rate-limit headers are read from the **raw** HTTP response (PR #35): the Groq SDK's
  parsed object strips them, so `_create_once` goes through
  `chat.completions.with_raw_response.create(...)` and `.parse()`. This is what lets
  `x-ratelimit-remaining-tokens` actually drive proactive pacing (`_pace`) and appear
  in retry logs; before #35 that read was a silent no-op in production.
- Per-run token cap: `_BudgetedClient(..., max_tokens=120000)` raises
  `LLMTokenBudgetExceeded`; `run_pipeline` catches it and PAUSES (does not fail) so
  jobs stay resumable.
- Daily-limit pause: a 429 that indicates a daily limit (or `Retry-After` > 120s)
  raises `GroqDailyLimitReached`; `run_pipeline` PAUSES with `paused`,
  `pause_reason="daily"`, `pause_message`, `pause_reset_seconds`. `/status` shows
  `Paused: Groq daily limit reached, resets in about Xh`. Per-UTC-day tokens are
  recorded in the `llm_usage` table (`add_llm_tokens` / `tokens_used_today`).
- Find-jobs guard (PR #31): `/find` refuses to start and renders
  `Groq daily limit reached, resets in about Xh` when today's tokens are >= 90% of
  `max_llm_tokens_per_day`, or a daily-limit pause is still in effect.

## Model verification (DONE)

- Live `GET https://api.groq.com/openai/v1/models` returns **403 (Cloudflare) unless a
  `User-Agent` header is sent**; with `User-Agent: atlas/1.0` it returns 200.
- Configured ids verified present live: extractor `openai/gpt-oss-120b`,
  evaluator `openai/gpt-oss-120b`, verifier `qwen/qwen3.8-27b`.
- Full live list (11): `allam-2-7b`, `canopylabs/orpheus-arabic-saudi`,
  `canopylabs/orpheus-v1-english`, `meta-llama/llama-prompt-guard-2-22m`,
  `meta-llama/llama-prompt-guard-2-86m`, `openai/gpt-oss-120b`,
  `openai/gpt-oss-20b`, `openai/gpt-oss-safeguard-20b`, `qwen/qwen3.8-27b`,
  `whisper-large-v3`, `whisper-large-v3-turbo`.

## Task D: YC jobs source

**Status: CLOSED by owner, ToS prohibits automated reading.**

The agent must not build a YC collector. The owner decided to close Task D because the applicable Terms of Service prohibit scraping / data mining / robots and commercial reproduction of the public jobs pages.

### Evidence cited (exact lines, verbatim)

#### robots.txt
URL: `https://www.ycombinator.com/robots.txt`

```
User-Agent: *
Disallow: /verify/*
Disallow: /library?categories=*&*
Allow: /library?categories=*
Disallow: /library?*
Disallow: /companies?*
Allow: /

User-Agent: facebookexternalhit
Allow: /verify/*

User-Agent: Twitterbot
Allow: /verify/*

User-Agent: LinkedInBot
Allow: /verify/*
```

URL: `https://www.workatastartup.com/robots.txt`

```
User-Agent: *
Disallow:
```

#### Terms of service
URL: `https://www.ycombinator.com/legal#tou` (Y Combinator Terms of Use; the "Site"
is defined as "the Y Combinator website (including all subdomains)"). Lines relied on:

> "obtain or attempt to access or otherwise obtain any materials or information
> through any means not intentionally made available or provided for through the Site."

> "**Commercial Use**: Unless otherwise expressly authorized herein or in the Site, you
> agree not to display, distribute, license, perform, publish, reproduce, duplicate,
> copy, create derivative works from, modify, sell, resell, exploit, transfer or upload
> for any commercial purposes, any portion of the Site, use of the Site, or access to
> the Site."

> "you agree not to modify, copy, frame, **scrape**, rent, lease, loan, sell, distribute
> or create derivative works based on the Site or the Site Content, in whole or in part
> ... In connection with your use of the Site you will not engage in or use any
> **data mining, robots, scraping or similar data gathering or extraction methods**. If
> you are blocked by Y Combinator from accessing the Site (including by blocking your IP
> address), you agree not to implement any measures to circumvent such blocking (e.g.,
> by masking your IP address or using a proxy IP address). Any use of the Site or the
> Site Content other than as specifically authorized herein is strictly prohibited."

URL: `https://www.workatastartup.com/terms` (Work at a Startup "Job Seeker User
Agreement"; the page's "Terms of Use" link points to
`https://www.ycombinator.com/legal/#tou`). Lines relied on:

> "You will not misuse Work at a Startup access for unauthorized commercial purposes or
> for your own gain."

> "This Job Seeker User Agreement is incorporated into and part of YC's Terms of Use."

**Finding:** robots.txt is permissive for both hosts (`/jobs` is not disallowed on YC; WaaS allows all), but the applicable Terms of Service explicitly prohibit scraping / data mining / robots and commercial reproduction, so automated reading of the public jobs pages is **not permitted by the ToS**.

### Alternatives (not built)
- **Add a job** (dashboard page): owner pastes JD text and link; never fetched (`source = "owner_added"`).
- **Company ATS pages in `companies.yaml`**: ingest from official job postings at known company ATS URLs if permitted by those terms (separate from scraping YC/WaaS lists).
- **Owner browsing**: owner manually adds relevant jobs.

## Planned and not built

Nothing below is implemented. Each needs the owner's go-ahead.

- **(a) Tailored resume builder.** Generator BUILT (PR #37; fact-ID grounding #39;
  claim-strength + coverage + evidence-linked skills #40; sentence-level facts +
  thin-section repair #44; empty-section guarantee, verbatim fallback, skills cap and
  on-demand cache in the current fix; `atlas/resume.py`, no UI);
  profile editor BUILT (PR #38; `/profile` captures internships, work, projects,
  education, links, certifications). It rewrites and reorders content drawn only from
  the approved profile; every generated line traces back to a profile fact, and unmet
  requirements are listed but never added. Still planned: the dashboard button, review
  page (diff vs base resume + per-line quote/source), owner approval and ATS-friendly
  single-column DOCX/PDF export (PR C2). No auto-submission.
- **(b) "Add a job" dashboard page.** BUILT (PR #33; background + progress page in
  PR #34). Owner pastes a JD's text and link; it is evaluated through the same
  pipeline with `source = "owner_added"`. The URL is **never fetched**.
- **(c) YC jobs source.** CLOSED by owner (ToS prohibits automated reading); see Task D.
  Alternatives: Add a job, company ATS pages in `companies.yaml`, owner browsing.
- **(d) Phase 8b assisted / auto apply.** Blocked until every gate is GO and the owner
  explicitly says so.
- **(e) Groq tier decision.** Decide between the paid Developer tier and staying on the
  ~200K tokens-per-day free limit.

## Owner-only

These cannot be done by the agent and are not marked PASS on the agent's own authority.

1. Label **40 real JDs** (>= 15 traps, >= 10 apply) from `eval/labeling_pool.jsonl` into
   `eval/labels.jsonl` and run `atlas label-check` (feeds G5 / DEC6). The agent must not
   create, edit, or relabel `eval/labels.jsonl`.
2. Rotate **ALL** keys (Groq, Gemini, Resend, Braintrust) and confirm (G1).
3. Run `mutmut` under WSL/Linux (G2 / T5).
4. Authorize the live `python -m atlas.cli eval --with-llm` run (G6, spend).
5. Sign off `review_sample.md` (G11).
6. One complete browser run: upload -> approve -> Find jobs to completion, with
   screenshots.

## How to run

Entry point is `python -m atlas.cli`. Commands:
`profile`, `review`, `status`, `run [--collect] [--limit]`, `decide [--limit]`,
`explain job_id`, `label-check [--labels] [--pool]`, `schedule`, `collect`,
`eval [--golden] [--with-llm]`, `selfcheck [--fast] [--full] [--report]`,
`feedback`, `dashboard [--host] [--port]`.

Gates (before any commit):

```
python -m pytest -q
ruff check atlas tests
black --check atlas tests
mypy atlas
python -m atlas.cli selfcheck --fast
```

Additional: `python -m atlas.cli selfcheck --full`, `python -m atlas.cli eval`,
`python -m atlas.cli label-check`.

## Limitations

- Golden set is synthetic (20 cases); the staged 70-JD pool is unlabelled, so every
  percentage is a smoke-test figure, not real accuracy. Precision@10 and seniority
  false-positive rate against real data are BLOCKED by G5.
- Source coverage gap: the staged pool is RemoteOK + Remotive only; Adzuna has no keys
  and Greenhouse/Lever/Ashby were unreachable from the sandbox.
- Verifier is a different model but the same provider family (Groq), not fully
  independent.
- `--fast` replaces LLM calls with fixtures; `--full` does not make live LLM calls, so
  live-LLM determinism is unverified.
- Mutation coverage unknown (T5 blocked).

## Owner decisions (standing)

- Fresher profile: ~3 months internship; experience tolerance 1 year.
- Interface: CLI + scheduled run PLUS web dashboard (email delivery cancelled).
- auto_applications/ bots disabled, kept for Phase 8.
- Merge workflow: always create a PR and merge it once CI is green; never wait for
  owner approval to merge; normal merge, admin bypass only if branch protection needs
  a review the author cannot self-provide.
- Sources: existing feeds + Adzuna + Greenhouse/Lever/Ashby; second Groq model as
  verifier.

## Starting a new session

1. Read AGENTS.md, PRD.md, and this HANDOFF.md.
2. Run `git log --oneline -10` and `git status -sb`.
3. Confirm they match this file before changing anything; if they disagree, say so and
   stop.
