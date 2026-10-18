# Atlas_OPS

A personal, single-user job-matching tool. It collects postings from job boards and ATS
pages, filters them with deterministic code, scores the survivors with an LLM, and
surfaces only roles that are a **genuine fit** for the candidate — with strict
seniority/experience filtering (the author is a fresher).

It is a CLI + scheduled pipeline **and** a local web dashboard. It optimises for
**precision and accuracy**, not scale or latency.

Two things it deliberately does **not** do:

- **It never applies anywhere.** There is no auto-apply, no form submission, no
  headless browser that talks to a job site. Every `APLD` / `NTAPLD` / `PEND` button
  only records what *you* clicked, in your own SQLite database.
- **It never rewrites your resume behind your back.** A tailored resume is generated on
  demand, shown to you for review and editing, and can only be exported as DOCX/PDF
  after you **approve** a version.

## What it does

1. Builds a **profile** once from a resume and/or a description, which you review and
   approve.
2. Collects, normalises and de-duplicates postings across sources.
3. Parses each JD (regex + LLM), applies **hard filters in code**, then skill-matches,
   evaluates, adversarially verifies and ranks each surviving job.
4. Decides **`apply` / `review` / `skip`** with vetoes, confidence and a full audit
   trail, and shows the results in a dashboard.
5. Optionally drafts a **tailored resume** for one job from your approved profile,
   grounded in your own profile facts, for you to review, edit, approve and export.

## Two input modes

The profile is built from one of (or both) — `InputMode` in `atlas/schemas.py`:

| Mode | Input | Notes |
|---|---|---|
| **`resume`** | a resume PDF, max 5 MB | parsed on `/setup`; text is never sent to telemetry |
| **`description`** | free text you paste (target roles, stack, location, seniority) | no upload needed |

`/setup` accepts a resume, a description, or both together. The approved profile is the
only thing the pipeline uses; it is never edited automatically after approval (any change
you make re-requires approval and re-runs the hard filters).

## Pipeline stages

```
Resume / description -> Profile (human-approved once)
                     -> Query planner -> Collectors -> normalise + dedupe (SQLite)
                     -> JD parser (regex + LLM) -> hard filters (code)
                     -> skill match (ontology) -> evaluator (LLM)
                     -> adversarial verifier (different model) -> ranker (weighted)
                     -> decision engine (apply/review/skip) -> digest + dashboard
                     -> feedback (bounded tuning)
```

Design rules: hard constraints first in deterministic code, soft scoring second with an
LLM; every LLM output is a validated typed object; the LLM judges but code decides; the
generator and the verifier are separate passes; every job is explainable at each stage;
and everything is measured against a labelled golden set.

`atlas/pipeline.py` is an explicit state machine, not a free-roaming agent loop. Each
stage logs to SQLite via `log_stage`, so any decision can be traced afterwards.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
pip install -r requirements-dev.txt   # test/dev extras
cp .env.example .env              # fill in real values, NEVER commit .env
```

Then edit `config.yaml` (preferences), `skills.yaml` (skill ontology) and
`companies.yaml` (target companies for the ATS collectors — all lists ship empty).

Start the dashboard:

```bash
python -m atlas.cli dashboard --port 8000
```

### Environment (`.env`)

Every variable below is documented in `.env.example`.

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | Groq LLM API key (the only required key) |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Adzuna job API |
| `RECRUITEE_CAREERS_SITES_TOKEN` | Recruitee `X-Careers-Sites-Token` header (required from 2027-02-10) |
| `RESEND_API_KEY` / `RESEND_FROM_EMAIL` / `RESEND_TO_EMAIL` | Resend email — **legacy; delivery is cancelled and no run path calls it** |
| `BRAINTRUST_API_KEY` | Optional. Empty = tracing is a local no-op and nothing leaves the machine |
| `BRAINTRUST_PROJECT` | Trace project name (default `My Project`) |
| `BRAINTRUST_PROJECT_ID` / `BRAINTRUST_ORG` | Project and org that receive traces and eval scores |
| `ATLAS_OFFLINE` | `1` forces telemetry off even with a key present |
| `DASHBOARD_PASSWORD` | Dashboard login; **required to bind a non-loopback host** |
| `ATLAS_DB` | SQLite path (defaults to `atlas.db`) |

### Observability (optional)

Every model call goes through `atlas.llm.LLMClient`, the single instrumentation point.
Only an explicit allowlist of non-PII fields is ever sent (stage/job identity, scores,
decisions, rule ids, model names, prompt versions, latency, token counts). Resume text,
`profile.json` contents, name, email, phone, prompt text and links other than the job URL
are dropped first. Telemetry failures can never fail a run.

## Dashboard

`python -m atlas.cli dashboard` serves a local web UI so the whole flow works without a
terminal. Pages:

| Page | Route | What it does |
|---|---|---|
| **Setup** | `/setup` | Upload a resume PDF (≤ 5 MB) and/or paste a description, then **Build profile** |
| **Profile** | `/profile` | Review/edit every field (level, years, skills, target roles, locations, internships, work, projects, education, links, certifications), then **Approve profile**. The pipeline stays blocked until a profile is approved |
| **Home** | `/` | **Find jobs** button (background run) plus the results table: only jobs that passed every hard filter with an `APPLY`/`REVIEW` decision, with score, reasons, risk, missing skills, seniority and `APLD`/`NTAPLD`/`PEND` bookkeeping |
| **Add job** | `/add` | Paste a JD's text and link; it runs the same pipeline as a background thread and redirects to `/add/progress`, which polls `/api/add-status` (current step, Groq rate-limit wait, calls/tokens). The URL is **never fetched** |
| **Rejected** | `/rejected` | Every hard-filter rejection with its rule, reason and quote, so false rejects are auditable |
| **Status** | `/status` | Live pipeline state: stage, counts, per-source yield and errors, LLM calls/tokens, ETA, and any pause message. JSON at `/api/status` |
| **Job page** | `/jobs/{id}` | Score line (`N / 100`), confidence, seniority, missing skills, per-dimension breakdown, vetoes with verbatim quotes, deduped reasons, **Prepare application** (opens the kit) and your own verdict form |
| **Application kit** | `/jobs/{id}/kit` | Claim extraction and evidence quotes for a single application (human drafting aid) |
| **Tailored resume** | `/jobs/{id}/resume` | Build, review (base vs tailored side by side, per-line fact citations, source badges, claim flags), edit, approve and export a grounded resume for that one job — see below |

Other routes: `/find` (POST, starts a run), `/health`, `/login` / `/logout`,
`/api/matched`. Only one run may be in flight at a time (a single-run lock).

### Password and same-origin protection

Set `DASHBOARD_PASSWORD` in `.env`; every route then requires a signed-cookie login
compared in constant time. With no password set the server **refuses to bind a
non-loopback address**, so it can only run on `127.0.0.1`. POSTs are rejected on a
cross-origin `Origin`/`Referer`, and all rendered values are HTML-escaped.

## Tailored resume

For one job at a time, on demand only (never inside a bulk run):

- **Grounded generation.** `atlas/resume.py` plus `prompts/tailored_resume.md` (version
  4) rewrite only what the approved profile already contains. Every summary sentence and
  bullet must cite a profile **fact id** (sentence-level for prose), so a line can never
  borrow unrelated detail. Claim strength (`led`, `owning`, `robust`, …) and any new
  technology or number are rejected unless a cited fact supports them. Skills are built in
  code (capped at 14, alias-deduped, evidence-linked) and never invented. Unmet
  requirements are listed as missing, never added. If the profile lacks detail the
  generator refuses instead of padding.
- **Cache and cost.** The cache key is `(profile hash, job id, prompt version, generator
  version)`; bump `GENERATOR_VERSION` when the rules change. A repeat build is free
  (0 tokens, 0 calls); **Regenerate** bypasses the cache. Every call is `temperature 0`
  inside a 3-call-per-resume budget.
- **Review and editing.** Warnings are deduped to one per section plus a count. Every line
  is editable; an edit is saved as a **new version** marked `owner-edited` (and stored with
  an empty `profile_hash` so the cache never serves it). An edit that names a technology or
  number absent from your profile shows a **warning, not a block**.
- **Approval, then export.** `GET /jobs/{id}/resume/export.docx|.pdf` returns
  **409 "Approve a version before exporting."** until you approve a version. The export is
  a single column of real, selectable text with the standard headings
  (Summary / Skills / Experience / Projects / Education), no tables, images, text boxes or
  header/footer, and the filename `Name_Role_Company`. Exported text is folded to plain
  ASCII at export time only, so the PDF core font never prints `?` for a non-breaking
  hyphen, dash, curly quote or accent; the stored text is unchanged.

## Groq limits and the daily-limit pause

Groq's free tier is roughly **30 requests/minute**, **1,000 requests/day**, **~8K
tokens/minute** and **~200K tokens/day**. Rate-limit 429s carry `Retry-After` and
`x-ratelimit-*` headers.

- **Caps** in `config.yaml` / `RunConfig`: `max_jobs_per_run: 12`,
  `max_llm_calls_per_run: 100`, `max_llm_tokens_per_run: 120000`,
  `max_llm_tokens_per_day: 200000`.
- **All retrying and pacing lives in our own wrapper**: the Groq SDK is built with
  `max_retries=0`, so its internal backoff cannot bypass our pacing, wait callbacks or
  daily-limit detection. Rate-limit headers are read from the raw HTTP response (the SDK's
  parsed object strips them), which is what drives proactive pacing and retry logging.
- **Daily-limit pause:** a 429 that means the daily limit (or `Retry-After` > 120 s) raises
  `GroqDailyLimitReached` and the run **pauses** (it does not fail, so jobs stay
  resumable). `/status` shows `Paused: Groq daily limit reached, resets in about Xh`, with
  per-UTC-day usage in the `llm_usage` table.
- **Find jobs refuses to start** when today's tokens are >= 90% of the daily cap or a
  daily-limit pause is still in effect.
- **Per-run token cap:** `max_llm_tokens_per_run` raises `LLMTokenBudgetExceeded`, which
  pauses rather than fails.

Because a resume run costs a few thousand tokens per call, a resume build runs in a
background thread with its own progress page that shows the current step, the Groq
rate-limit wait, and the tokens/calls spent.

## Safety rules

- **No auto-apply anywhere.** Phase 8 / assisted apply stays blocked until every
  pre-Phase-8 gate is GO *and* the owner explicitly says so. The legacy
  `auto_applications/` bots are retained but not imported by the pipeline.
- **Owner approval for resumes.** Nothing exports until a version is approved; editing
  never invents content.
- **The dashboard only records clicks.** `jobs.application_status` changes when you click
  `APLD`/`NTAPLD`/`PEND`; the tool never opens an application form.
- **`eval/labels.jsonl` is human-owned.** It is created, edited and relabelled only by the
  owner; `label-check` and `eval` read it and never write it, and the interactive
  `label` command appends one record only when the owner presses `a` or `s` (and
  rewrites only when they press `u`). The agent never writes this file.
- **No secrets in git.** `.env`, `atlas.db`, `uploads/`, `data/`, resumes and generated
  exports are gitignored and untracked.

## CLI

```bash
python -m atlas.cli profile --resume resume.pdf --describe "target roles..." --out profile.json
python -m atlas.cli review profile.json        # review + approve an existing profile.json
python -m atlas.cli status                     # show the latest saved profile
python -m atlas.cli collect                    # fetch + normalise + dedupe jobs
python -m atlas.cli run                        # parse -> filter -> evaluate -> verify -> rank -> digest
python -m atlas.cli run --collect --limit 50   # fetch first, then process
python -m atlas.cli decide                     # decision engine dry-run (no email)
python -m atlas.cli decide --limit 25          # dry-run over <= 25 jobs
python -m atlas.cli explain <job_id>           # print the latest stored decision for a job
python -m atlas.cli schedule --hour 9          # run once now, then daily at 09:00
python -m atlas.cli dashboard --port 8000      # web dashboard
python -m atlas.cli eval                       # golden-set evaluation (deterministic)
python -m atlas.cli eval --with-llm            # + live LLM precision@10 gate (costs tokens)
python -m atlas.cli feedback <job_id> good     # or: bad --reason <code>
python -m atlas.cli label-check                # validate a human-labelled labels file (read-only)
python -m atlas.cli label                      # interactively label pool jobs (owner: a=apply s=skip u=undo q=quit)
python -m atlas.cli selfcheck --fast           # deterministic pre-flight checks
python -m atlas.cli selfcheck --full           # + coverage / lint / types / mutation / network
```

`python -m atlas.cli --help` is the authoritative command list.

## Configuration

Preferences live in `config.yaml` (single source of truth). Skill aliases live in
`skills.yaml`; target ATS companies live in `companies.yaml`. See each file for its keys.

## Sources

Collectors are pluggable (`atlas/collectors/`) and enabled per-source in `config.yaml`
under `sources:`. Failures in one source never abort a run.

| Source | Type | Default | Needs |
|---|---|---|---|
| RemoteOK | feed | on | - |
| Remotive | feed | on | - |
| Adzuna | API | on | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` |
| Greenhouse | ATS board | on | token in `companies.yaml` |
| Lever | ATS board | on | token in `companies.yaml` |
| Ashby | ATS board | on | token in `companies.yaml` |
| Himalayas | API | on | - |
| Arbeitnow | API | on | - |
| Jobicy | API | on | - |
| We Work Remotely | RSS feed | off | opt-in |
| SmartRecruiters | ATS board | off | token, opt-in |
| Workable | ATS board | off | token, opt-in |
| Recruitee | ATS board | on | tenant in `companies.yaml`, `RECRUITEE_CAREERS_SITES_TOKEN` from 2027-02-10 |
| Teamtailor | ATS board | off | token, opt-in |

"on" means `sources.<name>: true` in the committed `config.yaml`; a source still runs
nothing until its requirement is met (ATS collectors only fetch the companies listed in
`companies.yaml`, which ships with every list empty). Turning a source on asserts you have
read its terms — see the per-source terms/licensing notes in the previous revision of this
file for the ones that still need an owner decision (`We Work Remotely`,
`SmartRecruiters`, `Workable`, `Teamtailor`).

Jobs are stripped of HTML, given a stable dedupe key, merged across boards,
freshness-filtered and stored in SQLite. When the same job appears on both an aggregator
and an ATS board, the ATS URL is canonical and every URL stays in `job.urls`.

### Resilience

`atlas/collectors/base.py` has two fetch paths: the original `default_get_json` (3
attempts, exponential backoff) and `HttpFetcher`, which adds an explicit `HttpPolicy`
(timeout, attempts, backoff bounds, a cap on how long a `Retry-After` can park you),
parses `Retry-After` in both delta-seconds and HTTP-date form, and serves raw text for
XML/Atom feeds. Transport failures, 5xx, 429 and 503 retry; a permanent 4xx does not.

Every source is covered by `tests/fixtures/<source>.json`, replayed offline by
`tests/replay.py` so no test touches the network.

## Decision engine

`atlas/decision.py` turns a verified evaluation into an explicit `apply` / `review` /
`skip` outcome with a full audit record. It reuses the evaluator's verdict and the
verifier's veto/downgrade and adds two LLM judgements: `project_relevance` and an
adversarial `decision_critic`.

- **Dimensions** (`skills_core`, `skills_secondary`, `education_fit`, `logistics_fit`,
  `project_relevance`, `role_fit`, `growth_fit`, `seniority_fit`) weighted by
  `config.yaml: decision.weights` (must sum to 1.0), with a bounded red-flag penalty.
- **Hard vetoes** force `skip`: coverage below `decision.coverage_floor`, insufficient
  years (1-year tolerance), `seniority_fit` below `decision.seniority_floor`, a verifier
  veto or a critic veto. Soft vetoes (a verifier downgrade) force `review`.
- **Rule** — `hard veto -> skip`; `soft veto or confidence < c_min -> review`;
  `score >= apply and confidence >= c_apply -> apply`; `score >= review -> review`;
  otherwise `skip`.
- **Audit** — each `Decision` stores dimension scores, weights, spreads, vetoes with
  verbatim evidence, `config_hash`, prompt versions and model names in the `decisions`
  table. All evidence quotes are validated verbatim against the JD/profile.

## Digest and scheduling

- **Digest** (`atlas/digest.py`) groups a run into **Strong matches**, **Worth a look**
  and **Needs review**, each with title, company, location, link, fit score, reasons,
  main risk, missing skills and seniority, plus the funnel summary.
- **Email delivery is cancelled.** `atlas.cli run` never sends mail; the digest is printed
  and served by the dashboard. `atlas/emailer.py` remains only for historical
  idempotency tests.
- **Scheduling** — `python -m atlas.cli schedule` runs once immediately, then daily at
  `schedule.daily_hour` (default 09:00 local). For unattended operation use Task
  Scheduler, cron/systemd or CI. Sending nothing when nothing clears the bar is a valid
  outcome.

## Feedback

```bash
python -m atlas.cli feedback <job_id> good
python -m atlas.cli feedback <job_id> bad --reason too_senior
python -m atlas.cli feedback --export        # append labelled cases to eval/feedback.jsonl
```

Reason codes: `too_senior`, `wrong_stack`, `bad_company`, `wrong_location`, `other`,
`good_fit`. Feedback becomes **bounded** tuning (`atlas/feedback.py`): bad companies are
blacklisted, good companies get a rank bonus, weights are nudged then renormalised.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest                             # unit tests (offline; no network)
python -m atlas.cli selfcheck --fast         # pre-flight checks
python -m atlas.cli eval                     # golden-set evaluation
python eval/eval.py                          # standalone golden-set eval
```

`selfcheck` runs the pre-Phase-8 audit checks (hygiene, secrets, gitignore, pinned deps,
README, tests, coverage, lint/format/types, dead code, network isolation, pipeline
invariants) and exits non-zero if anything is FAIL or BLOCKED. `--full` adds
coverage/lint/type/mutation/network checks; `--report out.json` writes the
machine-readable result.

The golden set lives in `eval/golden_set.jsonl`; the human-labelled set lives in
`eval/labels.jsonl` (owner-owned) and is validated by `label-check`. Labels are
produced with `python -m atlas.cli label`, which shows one pool job at a time (title,
company, location, remote type, source, posted date, URL, full description - never a
score or a decision) in a fixed-seed random order, appends
`{pool_id, url, label, reason, labeled_by}` on the owner's keypress, skips anything
already labelled so a session is resumable, and ends with the same report as
`label-check`.

## Project layout

```
atlas/             # pipeline package (incl. atlas/collectors/, atlas/export.py)
atlas/dashboard.py # local web UI
prompts/           # versioned prompt files
config.yaml        # preferences
skills.yaml        # skill ontology / alias map
companies.yaml     # target company ATS boards
eval/              # golden set, skill cases, labels, eval harness
tests/             # unit + replay tests
auto_applications/ # legacy Playwright bots, retained for Phase 8 (disabled)
```

## Security

- Never commit `.env`, `atlas.db`, `uploads/`, `data/`, resumes or generated exports
  (all gitignored).
- Rotate any key that was ever committed.
- The dashboard binds loopback unless `DASHBOARD_PASSWORD` is set.

## License

MIT — see [LICENSE](LICENSE).
