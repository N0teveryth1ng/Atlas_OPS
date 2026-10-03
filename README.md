# Atlas_OPS

Personal, single-user job-matching tool. Given a resume and a set of
preferences, it finds, filters, ranks, and emails only jobs that are a
**genuine fit** — with strict seniority/experience filtering (the author is a
fresher).

Optimised for **precision and accuracy**, not scale or latency.

> Status: under active rework. The legacy FastAPI prototype has been retired;
> the tool is now a CLI + scheduled pipeline under `atlas/`. See the phased
> plan below.

## Goal / success metrics

- **Precision@10 >= 80%** — of the top 10 jobs emailed, at least 8 are
  applications worth making.
- **Seniority false-positive rate ~= 0%** — no role requiring more experience
  than the candidate qualifies for gets through.
- Every emailed job carries a reason it matched; every rejected job logs a
  rejection reason.

## Architecture (target)

```
Resume/description -> Profile Agent -> profile.json (human-approved once)
                    -> Query Planner -> Collectors -> Normalize + Dedupe (SQLite)
                    -> JD Parser (regex + LLM) -> Hard Filter (code)
                    -> Skill Match -> Evaluator (LLM) -> Verifier (adversarial)
                    -> Ranker -> Digest + Email -> Feedback
```

Design rules:

1. Hard constraints first (deterministic code), soft scoring second (LLM).
2. Structured everything — every LLM output is a validated typed object.
3. LLM understands and judges; code makes the final decisions.
4. Generator and verifier are separate passes.
5. Every job is explainable — why it passed or failed at each stage.
6. Measured against a labelled golden set before/after every change.
7. Human stays in the loop for applying (no auto-submit during the rework).

## Phased roadmap

- **Phase 0** — audit + repo hygiene + config/logging skeleton. *(current)*
- **Phase 1** — Pydantic schemas, LLM client, config, SQLite, input modes.
- **Phase 2** — structured JD parsing + hard filters + golden set.
- **Phase 3** — skill ontology / alias map + weighted matching.
- **Phase 4** — collectors, query planner, normalize, dedupe.
- **Phase 5** — evaluator + adversarial verifier.
- **Phase 6** — ranker, digest, email, scheduling.
- **Phase 7** — feedback loop + tuning.
- **Phase 8 (deferred)** — tailoring / assisted applying.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
cp .env.example .env              # fill in real values, never commit .env
```

### Environment (`.env`)

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | Groq LLM API key |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Adzuna job API (planned) |
| `RESEND_API_KEY` | Resend email API key |
| `RESEND_FROM_EMAIL` | From address for digests |
| `RESEND_TO_EMAIL` | Digest recipient |

### Configuration

Preferences live in `config.yaml` (single source of truth). See the file for
all keys.

## Project layout

```
atlas/            # pipeline package
config.yaml       # user preferences
companies.yaml    # target company list (planned)
skills.yaml       # skill ontology / alias map (planned)
prompts/          # versioned prompt files
eval/             # golden set + eval harness (planned)
tests/            # unit tests
auto_applications/# legacy Playwright bots, retained for Phase 8 (disabled)
```

## CLI

```bash
python -m atlas.cli profile --resume resume.pdf --describe "target roles..." --out profile.json
python -m atlas.cli review profile.json     # review + approve
python -m atlas.cli status
```

## Security

- Never commit `.env`, `uploads/`, resumes, or the SQLite DB.
- Rotate any key that was ever committed.
