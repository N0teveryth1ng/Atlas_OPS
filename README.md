# Atlas

Atlas is a local job-matching tool that collects postings, checks them against an approved candidate profile, and presents the strongest matches with clear evidence.

It combines deterministic filters with structured LLM scoring. Results and decisions are stored locally in SQLite and reviewed in a small web dashboard.

## Demo

[Watch a short walkthrough](https://github.com/user-attachments/assets/aa0e7bbe-1948-4ed5-a7b3-48563e0a90f9)

## What it does

- Builds a candidate profile from a resume PDF, a written description, or both.
- Collects and deduplicates job postings from configured job boards and company career pages.
- Filters out roles that miss hard requirements, then scores and explains the remaining matches.
- Tracks application decisions and shows rejected jobs with their reasons.
- Prepares an optional, profile-grounded resume for a selected job. You review and approve it before export.

Atlas does not submit applications. Application status is updated only when you record it yourself.

## Quick start

Requires Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Add an LLM API key to `.env`. When both are available, Atlas prefers `GEMINI_API_KEY`; otherwise it uses `GROQ_API_KEY`. Configure job preferences in `config.yaml` and, if using company career pages, add target boards in `companies.yaml`.

Start the dashboard:

```powershell
python -m atlas.cli dashboard
```

Open <http://127.0.0.1:8000>. The dashboard stays on your computer by default. Set `DASHBOARD_PASSWORD` in `.env` before exposing it on a non-local address.

## First run

1. Open **Setup**, add a resume and/or a short description of your target roles, skills, and locations, then build your profile.
2. Review and approve the profile. Update any fields that need correction.
3. Choose **Find jobs** to collect and evaluate postings.
4. Review matches, explanations, and rejected jobs. Record application status manually.

You can also enter a posting through **Add job**. Atlas evaluates its text without fetching the supplied URL.

## Configuration

| File or variable | Purpose |
| --- | --- |
| `config.yaml` | Candidate preferences, filters, model names, run limits, and enabled sources |
| `skills.yaml` | Skill names and aliases used for matching |
| `companies.yaml` | Company career pages used by ATS collectors |
| `GEMINI_API_KEY` | Gemini API key; preferred when set |
| `GROQ_API_KEY` | Groq API key; used when Gemini is not configured |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Optional Adzuna access |
| `DASHBOARD_PASSWORD` | Optional dashboard login; required for non-local binding |
| `ATLAS_DB` | Optional SQLite database path; defaults to `atlas.db` |

See [`.env.example`](.env.example) for the full environment template. Keep `.env`, resumes, databases, and generated exports out of version control.

## Command line

```bash
python -m atlas.cli profile --resume resume.pdf --describe "Backend roles in India"
python -m atlas.cli review profile.json
python -m atlas.cli status
python -m atlas.cli collect
python -m atlas.cli run
python -m atlas.cli schedule --hour 9
python -m atlas.cli dashboard
```

Useful evaluation and feedback commands:

```bash
python -m atlas.cli eval
python -m atlas.cli feedback <job_id> good
python -m atlas.cli selfcheck --fast
```

Run `python -m atlas.cli --help` for the complete command list. The scheduler runs once immediately, then daily at the configured local hour.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
python -m atlas.cli selfcheck --fast
```

Tests use local fixtures and do not require live job-board requests. Evaluation with live LLM calls is separate from the default offline test run.

## Data and safety

- Job, profile, and decision records are stored in the local SQLite database.
- A profile must be reviewed and approved before it is used for matching.
- Generated resumes use profile facts and require your approval before export.
- Atlas never submits applications or sends application forms.
- Optional Braintrust tracing is disabled when `BRAINTRUST_API_KEY` is empty; set `ATLAS_OFFLINE=1` to force it off.

## License

MIT. See [LICENSE](LICENSE).
