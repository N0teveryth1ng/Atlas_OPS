"""Pre-Phase-8 self-check runner (audit ``Atlas_OPS_preflight_prompt.md``).

Runs a fixed battery of automated checks and prints one line per check::

    ID | name | PASS/FAIL/BLOCKED | evidence

Exit code is non-zero if any check is FAIL or BLOCKED. Fast mode runs only the
deterministic, in-process checks (no network, no real email, no subprocess). Full
mode additionally runs the external toolchain (pytest, coverage, ruff, black,
mypy, vulture, socket-disabled suite) and the git-history secret scan.

Nothing here sends email, applies to jobs, or spends money. The email check uses
an injected fake transport; the end-to-end check uses an injected fake LLM.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .config import REPO_ROOT, get_settings
from .prompt_store import load_prompt

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
SKIPPED = "SKIPPED"

# Coverage floor (audit T2) per high-risk module.
COVERAGE_FLOORS: dict[str, int] = {
    "atlas/filters.py": 90,
    "atlas/jd_parser.py": 90,
    "atlas/skills.py": 90,
    "atlas/ranker.py": 90,
    "atlas/digest.py": 90,
    "atlas/emailer.py": 90,
    "atlas/feedback.py": 90,
    "atlas/llm.py": 90,
}

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("groq_api_key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    ("resend_api_key", re.compile(r"re_[A-Za-z0-9]{16,}")),
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("phone_in", re.compile(r"\+91[\s-]?\d{10}")),
    ("windows_user_path", re.compile(r"C:\\Users\\[^\"'\s]+")),
    ("resume_path", re.compile(r"Soham[_\s]Das", re.IGNORECASE)),
]

REQUIRED_IGNORES = [
    ".env",
    "__pycache__/",
    "uploads/",
    "*.db",
    "logs/",
    "profile.json",
    "eval/feedback.jsonl",
    "selfcheck_report.json",
    ".coverage",
]

SENSITIVE_TRACKED = re.compile(
    r"(^|/)(\.env$|.*\.(pem|key|p12|pfx)$|profile\.json$|.*\.db$|.*\.sqlite3?$)",
    re.IGNORECASE,
)


@dataclass
class CheckResult:
    id: str
    name: str
    status: str
    evidence: str
    mode: str = "fast"
    detail: list[str] = field(default_factory=list)


@dataclass
class SelfCheckReport:
    generated_at: str
    mode: str
    git_sha: str
    config_hash: str
    prompt_versions: dict[str, str]
    models: dict[str, str]
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if all(c.status in {PASS, SKIPPED} for c in self.checks) else 1

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {PASS: 0, FAIL: 0, BLOCKED: 0, SKIPPED: 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def to_dict(self) -> dict:
        data = asdict(self)
        data["summary"] = self.summary()
        data["exit_code"] = self.exit_code
        return data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _config_hash() -> str:
    digest = hashlib.sha256()
    for name in ("config.yaml", "skills.yaml"):
        path = REPO_ROOT / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _prompt_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for path in sorted((REPO_ROOT / "prompts").glob("*.md")):
        try:
            _, version = load_prompt(path.stem)
        except Exception:  # noqa: BLE001
            version = "unreadable"
        versions[path.stem] = version
    return versions


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        out = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        combined = (out.stdout or "") + (out.stderr or "")
        return out.returncode, combined
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"


def _module_available(name: str) -> bool:
    return (
        subprocess.run(
            [sys.executable, "-c", f"import {name}"], capture_output=True, check=False
        ).returncode
        == 0
    )


def _ok(cid: str, name: str, evidence: str, detail: list[str] | None = None, mode: str = "fast"):
    return CheckResult(cid, name, PASS, evidence, mode, detail or [])


def _bad(cid: str, name: str, evidence: str, detail: list[str] | None = None, mode: str = "fast"):
    return CheckResult(cid, name, FAIL, evidence, mode, detail or [])


def _blocked(cid: str, name: str, evidence: str, mode: str = "fast"):
    return CheckResult(cid, name, BLOCKED, evidence, mode, [])


# --------------------------------------------------------------------------- #
# Fast checks (in-process, deterministic)
# --------------------------------------------------------------------------- #


def check_h1_working_tree(_: dict) -> CheckResult:
    branch_rc, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    status_rc, status = _run(["git", "status", "--porcelain"])
    if branch_rc != 0 or status_rc != 0:
        return _blocked("H1", "working tree clean, on main, current", "git unavailable")
    branch = branch.strip()
    problems: list[str] = []
    if status.strip():
        problems.append(f"dirty ({len(status.splitlines())} path(s))")
    if branch != "main":
        problems.append(f"on branch '{branch}', not 'main'")
    if not problems:
        behind_rc, behind = _run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"]
        )
        if behind_rc == 0 and behind.strip():
            _left, _sep, right = behind.strip().partition("\t")
            if right.strip() and right.strip() != "0":
                problems.append(f"{right.strip()} commit(s) behind origin/main")
    if problems:
        return _bad("H1", "working tree clean, on main, current", "; ".join(problems), problems[:5])
    return _ok("H1", "working tree clean, on main, current", f"clean, main @ {_git_sha()[:12]}")


def check_h2_tree_secrets(_: dict) -> CheckResult:
    tracked = _tracked_files()
    hits = [f"tracked:{f}" for f in tracked if SENSITIVE_TRACKED.search(f)]
    for rel in tracked:
        path = REPO_ROOT / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        text = _read_text(path)
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(f"{rel}:{label}")
    if hits:
        return _bad("H2", "no secrets in the working tree", f"hits: {hits}", hits)
    return _ok("H2", "no secrets in the working tree", f"scanned {len(tracked)} tracked paths")


def check_h4_gitignore(_: dict) -> CheckResult:
    text = _read_text(REPO_ROOT / ".gitignore")
    missing = [p for p in REQUIRED_IGNORES if p not in text]
    if missing:
        return _bad("H4", "gitignore covers sensitive/generated paths", f"missing: {missing}")
    return _ok(
        "H4",
        "gitignore covers sensitive/generated paths",
        f"{len(REQUIRED_IGNORES)} entries present",
    )


def check_h5_env_connascence(_: dict) -> CheckResult:
    used: set[str] = set()
    pattern = re.compile(r"os\.environ(?:\.get)?\(\s*[\"']([A-Z0-9_]+)[\"']")
    for path in (REPO_ROOT / "atlas").rglob("*.py"):
        used.update(pattern.findall(_read_text(path)))
    documented = set()
    for line in _read_text(REPO_ROOT / ".env.example").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            documented.add(line.split("=", 1)[0].strip())
    undocumented = sorted(used - documented)
    if undocumented:
        return _bad(
            "H5", "every env var documented in .env.example", f"undocumented: {undocumented}"
        )
    return _ok("H5", "every env var documented in .env.example", f"{len(used)} vars documented")


def check_h6_requirements_pinned(_: dict) -> CheckResult:
    unpinned: list[str] = []
    for req in ("requirements.txt", "requirements-dev.txt"):
        for line in _read_text(REPO_ROOT / req).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "==" not in line:
                unpinned.append(f"{req}:{line}")
    if unpinned:
        return _bad("H6", "requirements are pinned", f"unpinned: {unpinned}", unpinned)
    return _ok("H6", "requirements are pinned", "all dependencies pinned")


def check_h7_readme(_: dict) -> CheckResult:
    text = _read_text(REPO_ROOT / "README.md")
    if not text:
        return _bad(
            "H7", "README documents setup/config/CLI/eval/selfcheck/schedule", "README.md missing"
        )
    required = [
        "pip install",
        "python -m atlas.cli profile",
        "python -m atlas.cli review",
        "python -m atlas.cli status",
        "python -m atlas.cli collect",
        "python -m atlas.cli run",
        "python -m atlas.cli schedule",
        "python -m atlas.cli eval",
        "python -m atlas.cli feedback",
        "selfcheck",
        "config.yaml",
    ]
    missing = [term for term in required if term not in text]
    if missing:
        return _bad(
            "H7", "README documents setup/config/CLI/eval/selfcheck/schedule", f"missing: {missing}"
        )
    return _ok(
        "H7",
        "README documents setup/config/CLI/eval/selfcheck/schedule",
        f"{len(required)} terms present",
    )


def check_a1_autoapply_isolation(_: dict) -> CheckResult:
    forbidden = ("auto_applications", "resend_mail")
    offenders: list[str] = []
    for path in (REPO_ROOT / "atlas").rglob("*.py"):
        try:
            tree = ast.parse(_read_text(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in forbidden:
                    offenders.append(f"{path.relative_to(REPO_ROOT)} imports {name}")
    if offenders:
        return _bad("A1", "auto-apply code not imported by pipeline", f"imports: {offenders}")
    return _ok(
        "A1", "auto-apply code not imported by pipeline", "no pipeline imports of legacy bots"
    )


def check_a2_no_application_posts(_: dict) -> CheckResult:
    forbidden = ("playwright", "selenium", "auto_apply", "apply_now", "submit_application")
    offenders: list[str] = []
    for path in (REPO_ROOT / "atlas").rglob("*.py"):
        if path.name == "selfcheck.py":
            continue  # this scanner mentions the tokens by design
        text = _read_text(path)
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{token}")
    if offenders:
        return _bad("A2", "pipeline cannot POST to application endpoints", f"tokens: {offenders}")
    return _ok(
        "A2", "pipeline cannot POST to application endpoints", "no browser/apply tokens in atlas/"
    )


def check_a3_no_submit_flag(_: dict) -> CheckResult:
    rc, output = _run([sys.executable, "-m", "atlas.cli", "run", "--help"], timeout=60)
    if rc != 0:
        return _blocked("A3", "atlas run has no submit/apply flag", f"--help rc={rc}")
    offenders = [t for t in ("--submit", "--apply", "--auto-apply", "--auto_apply") if t in output]
    if offenders:
        return _bad("A3", "atlas run has no submit/apply flag", f"flags: {offenders}")
    return _ok("A3", "atlas run has no submit/apply flag", "no submit/apply option exposed")


def check_a4_tracked_pii(_: dict) -> CheckResult:
    hits: list[str] = []
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        text = _read_text(path)
        for label, pattern in PII_PATTERNS:
            if pattern.search(text):
                hits.append(f"{rel}:{label}")
    if hits:
        return _bad("A4", "no hard-coded PII in tracked files", f"hits: {hits}", hits)
    return _ok("A4", "no hard-coded PII in tracked files", "no PII patterns matched")


def check_g5_golden_provenance(_: dict) -> CheckResult:
    path = REPO_ROOT / "eval" / "golden_set.jsonl"
    if not path.exists():
        return _blocked(
            "G5", "golden set is real + human-labelled", "eval/golden_set.jsonl missing"
        )
    cases = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    problems: list[str] = []
    if len(cases) < 40:
        problems.append(f"count {len(cases)} < 40")
    for case in cases:
        for fieldname in ("source", "url", "fetched_at", "labeled_by"):
            if fieldname not in case:
                problems.append(f"{case.get('id', '?')} missing '{fieldname}'")
                break
        else:
            if case.get("labeled_by") != "human":
                problems.append(f"{case.get('id', '?')} labeled_by != human")
    if problems:
        return _blocked(
            "G5",
            "golden set is real + human-labelled",
            f"{len(cases)} cases; {'; '.join(problems[:4])}",
        )
    return _ok("G5", "golden set is real + human-labelled", f"{len(cases)} human-labelled cases")


def check_g3_seniority_matrix(_: dict) -> CheckResult:
    path = REPO_ROOT / "tests" / "seniority_matrix_cases.json"
    if not path.exists():
        return _bad("G3", "seniority/year matrix >=150 cases", "fixture missing")
    cases = json.loads(path.read_text(encoding="utf-8"))
    if len(cases) < 150:
        return _bad("G3", "seniority/year matrix >=150 cases", f"only {len(cases)} cases")
    rc, output = _run([sys.executable, "-m", "pytest", "tests/test_seniority_matrix.py", "-q"])
    if rc != 0:
        return _bad("G3", "seniority/year matrix >=150 cases", f"pytest rc={rc}", [output[-500:]])
    return _ok("G3", "seniority/year matrix >=150 cases", f"{len(cases)} cases pass")


def check_l5_verifier_invariant(_: dict) -> CheckResult:
    from .schemas import Recommendation, Verdict, VerifierVerdict
    from .verifier import resolve_recommendation

    rank = {
        Recommendation.skip: 0,
        Recommendation.maybe: 1,
        Recommendation.apply: 2,
        Recommendation.strong_apply: 3,
    }
    problems: list[str] = []
    for rec in Recommendation:
        for veto in (False, True):
            for down in (None, *Recommendation):
                verdict = Verdict(fit_score=50, recommendation=rec)
                verifier = VerifierVerdict(veto=veto, downgrade_to=down)
                final = resolve_recommendation(verdict, verifier)
                if veto and final != Recommendation.skip:
                    problems.append(f"veto ignored for {rec}")
                if not veto and rank[final] > rank[rec]:
                    problems.append(f"upgraded {rec}->{final}")
                if (
                    down is not None
                    and not veto
                    and rank[final] > rank[down]
                    and rank[down] < rank[rec]
                ):
                    problems.append(f"downgrade_to not applied {rec}->{final}")
    if problems:
        return _bad("L5", "verifier can only downgrade/veto", "; ".join(sorted(set(problems))[:4]))
    return _ok("L5", "verifier can only downgrade/veto", "all 4x2x5 combinations bounded")


def check_d3_quote_validation(_: dict) -> CheckResult:
    from .evidence import validate_verdict_evidence
    from .schemas import Evidence, EvidenceSource, Recommendation, Verdict

    jd_text = "We build backend services in Python and ship to production."
    good = Verdict(
        fit_score=70,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="backend services in Python", source=EvidenceSource.jd)],
    )
    if validate_verdict_evidence(good, jd_text, ""):
        return _bad("D3", "quotes must appear in source", "verbatim quote rejected")
    bad = Verdict(
        fit_score=70,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="fabricated claim never written", source=EvidenceSource.jd)],
    )
    if not validate_verdict_evidence(bad, jd_text, ""):
        return _bad("D3", "quotes must appear in source", "fabricated quote accepted")
    return _ok(
        "D3", "quotes must appear in source", "present quote accepted, absent quote rejected"
    )


def check_d4_status_machine(_: dict) -> CheckResult:
    from .job_status import SHIPPABLE, IllegalTransition, JobStatus, transition

    problems: list[str] = []
    for bad_status in (JobStatus.rejected, JobStatus.needs_review, JobStatus.new):
        if bad_status in SHIPPABLE:
            problems.append(f"{bad_status.value} shippable")
    if not {JobStatus.verified, JobStatus.ranked} <= SHIPPABLE:
        problems.append("verified/ranked not shippable")
    try:
        transition(JobStatus.new, JobStatus.ranked)
    except IllegalTransition:
        pass
    else:
        problems.append("new->ranked allowed")
    if transition(JobStatus.new, JobStatus.parsed) != JobStatus.parsed:
        problems.append("new->parsed blocked")
    if problems:
        return _bad("D4", "job status machine guards shipping", "; ".join(problems))
    return _ok("D4", "job status machine guards shipping", "illegal transitions blocked")


def check_d1_email_idempotency(_: dict) -> CheckResult:
    from .config import Settings
    from .db import (
        connect,
        emailed_job_ids,
        init_db,
        mark_jobs_emailed,
        start_run,
        upsert_job,
    )
    from .digest import build_digest
    from .emailer import ResendEmailer, send_digest
    from .job_status import JobStatus
    from .pipeline import ProcessedJob
    from .schemas import (
        Evidence,
        EvidenceSource,
        FilterResult,
        Job,
        Recommendation,
        Verdict,
        VerifierVerdict,
    )
    from .skills import SkillMatch

    conn = connect(":memory:")
    init_db(conn)
    run_id = start_run(conn, "selfcheck")
    job = Job(
        source="selfcheck",
        title="Backend Engineer",
        company="Acme",
        url="https://x/1",
        dedupe_key="selfcheck-d1",
    )
    job_id, _created = upsert_job(conn, run_id, job)
    result = ProcessedJob(
        job_id=job_id,
        job=job,
        filter_result=FilterResult(passed=True),
        verdict=Verdict(
            fit_score=80,
            recommendation=Recommendation.apply,
            reasons_for=[Evidence(quote="Backend Engineer", source=EvidenceSource.jd)],
        ),
        verifier=VerifierVerdict(veto=False),
        skill_match=SkillMatch(must_have_coverage=1.0, nice_to_have_coverage=1.0),
        final_recommendation=Recommendation.apply,
        score=80.0,
        status=JobStatus.ranked,
    )
    sent_payloads: list[Any] = []

    class _Resp:
        status_code = 200
        text = "ok"

    def fake_transport(method, url, *, headers, json):
        sent_payloads.append(json)
        return _Resp()

    settings = Settings()
    digest = build_digest([result], settings, run_id=run_id, already_sent=emailed_job_ids(conn))
    if digest.is_empty():
        conn.close()
        return _bad("D1", "email idempotency", "digest unexpectedly empty")
    emailer = ResendEmailer("k", "a@b.c", "d@e.f", transport=fake_transport)
    send_digest(digest, settings, emailer=emailer)
    mark_jobs_emailed(conn, digest.job_ids())
    again = build_digest([result], settings, run_id=run_id, already_sent=emailed_job_ids(conn))
    conn.close()
    if not again.is_empty() or len(sent_payloads) != 1:
        return _bad(
            "D1", "email idempotency", f"resent={not again.is_empty()} sends={len(sent_payloads)}"
        )
    return _ok("D1", "email idempotency", "second pass sends nothing")


def check_e2e_dryrun(_: dict) -> CheckResult:
    from .config import Settings
    from .db import (
        connect,
        get_latest_profile,
        init_db,
        save_profile,
        start_run,
        upsert_job,
    )
    from .digest import build_digest
    from .pipeline import run_pipeline
    from .schemas import (
        CandidateProfile,
        Evidence,
        EvidenceSource,
        ExperienceLevel,
        Job,
        Proficiency,
        Recommendation,
        Skill,
    )

    class FakeLLM:
        def call_json(self, *, schema, system, user, model=None, temperature=None):
            name = schema.__name__
            if name == "ParsedJD":
                return schema(min_years_experience=1, must_have_skills=["Python"], confidence=0.95)
            if name == "Verdict":
                return schema(
                    fit_score=85,
                    recommendation=Recommendation.apply,
                    reasons_for=[
                        Evidence(quote="Junior Python Developer", source=EvidenceSource.jd)
                    ],
                )
            if name == "VerifierVerdict":
                return schema(veto=False)
            return schema()

    settings = Settings()
    conn = connect(":memory:")
    init_db(conn)
    profile = CandidateProfile(
        total_experience_months=0,
        experience_level=ExperienceLevel.fresher,
        skills=[Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
        approved=True,
    )
    save_profile(conn, profile)
    run_id = start_run(conn, "selfcheck")
    upsert_job(
        conn,
        run_id,
        Job(
            source="selfcheck",
            title="Junior Python Developer",
            company="Acme",
            url="https://x/2",
            description_raw="Junior Python Developer, 1+ years of experience.",
        ),
    )
    stored = get_latest_profile(conn)
    if stored is None:
        conn.close()
        return _bad("E2E", "end-to-end dry run (fake LLM, no network)", "no stored profile")
    results = run_pipeline(conn, run_id, stored, settings, cast(Any, FakeLLM()))
    digest = build_digest(results, settings, run_id=run_id)
    conn.close()
    if not results or digest.is_empty():
        return _bad("E2E", "end-to-end dry run (fake LLM, no network)", "no digest items produced")
    return _ok(
        "E2E",
        "end-to-end dry run (fake LLM, no network)",
        f"{len(results)} processed, {digest.item_count} digest items",
    )


FAST_CHECKS: list[Callable[[dict], CheckResult]] = [
    check_h1_working_tree,
    check_h2_tree_secrets,
    check_h4_gitignore,
    check_h5_env_connascence,
    check_h6_requirements_pinned,
    check_h7_readme,
    check_a1_autoapply_isolation,
    check_a2_no_application_posts,
    check_a3_no_submit_flag,
    check_a4_tracked_pii,
    check_g5_golden_provenance,
    check_g3_seniority_matrix,
    check_l5_verifier_invariant,
    check_d3_quote_validation,
    check_d4_status_machine,
    check_d1_email_idempotency,
    check_e2e_dryrun,
]


# --------------------------------------------------------------------------- #
# Full checks (external toolchain)
# --------------------------------------------------------------------------- #


def check_h3_history_secrets(_: dict) -> CheckResult:
    try:
        out = subprocess.run(
            ["git", "log", "--all", "-p"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _blocked("H3", "no secrets in git history", "history scan timed out")
    if out.returncode != 0:
        return _blocked("H3", "no secrets in git history", "git log failed")
    hits = [label for label, pattern in SECRET_PATTERNS if pattern.search(out.stdout or "")]
    if hits:
        return _bad("H3", "no secrets in git history", f"patterns: {hits}", hits)
    return _ok("H3", "no secrets in git history", "no secret patterns in full history")


def check_t1_tests(_: dict) -> CheckResult:
    rc, output = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly"], timeout=600)
    tail = [line for line in output.splitlines() if line.strip()][-1:] or [""]
    if rc != 0:
        return _bad("T1", "test suite passes", f"pytest rc={rc}: {tail[0]}", [output[-800:]])
    return _ok("T1", "test suite passes", tail[0], mode="full")


def check_t2_coverage(_: dict) -> CheckResult:
    rc, _output = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "--cov=atlas",
            "--cov-branch",
            "--cov-report=json:coverage.json",
        ],
        timeout=900,
    )
    cov_path = REPO_ROOT / "coverage.json"
    if not cov_path.exists():
        return _blocked(
            "T2", "coverage floors (>=90% high-risk modules)", f"coverage report missing (rc={rc})"
        )
    data = json.loads(cov_path.read_text(encoding="utf-8"))
    files = data.get("files", {})
    below: list[str] = []
    for rel, floor in COVERAGE_FLOORS.items():
        info = files.get(rel) or files.get(rel.replace("/", "\\"))
        if not info:
            below.append(f"{rel}=missing")
            continue
        pct = info["summary"]["percent_covered"]
        if pct < floor:
            below.append(f"{rel}={pct:.0f}%<{floor}%")
    if below:
        return _bad(
            "T2", "coverage floors (>=90% high-risk modules)", "; ".join(below), below, mode="full"
        )
    return _ok("T2", "coverage floors (>=90% high-risk modules)", "all floors met", mode="full")


def check_t3_lint_format_types(_: dict) -> CheckResult:
    problems: list[str] = []
    ruff = _module_available("ruff")
    black = _module_available("black")
    mypy = _module_available("mypy")
    if ruff:
        rc, _out = _run([sys.executable, "-m", "ruff", "check", "atlas", "tests"], timeout=300)
        if rc != 0:
            problems.append("ruff")
    else:
        problems.append("ruff:unavailable")
    if black:
        rc, _out = _run([sys.executable, "-m", "black", "--check", "atlas", "tests"], timeout=300)
        if rc != 0:
            problems.append("black")
    else:
        problems.append("black:unavailable")
    if mypy:
        rc, _out = _run([sys.executable, "-m", "mypy", "atlas"], timeout=300)
        if rc != 0:
            problems.append("mypy")
    else:
        problems.append("mypy:unavailable")
    if problems:
        return _bad(
            "T3", "ruff + black + mypy clean", f"failing: {problems}", problems, mode="full"
        )
    return _ok("T3", "ruff + black + mypy clean", "all three clean", mode="full")


def _pytest_signature(output: str) -> str:
    last = [line for line in output.splitlines() if line.strip()][-1:][0]
    return re.sub(r"\s+in\s+[\d.]+s", "", last).strip()


def check_t4_flaky(_: dict) -> CheckResult:
    signatures: list[str] = []
    for _run_index in range(3):
        rc, output = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly"], timeout=600)
        if rc != 0:
            return _bad(
                "T4",
                "suite is not order/run dependent",
                f"run rc={rc}",
                [output[-400:]],
                mode="full",
            )
        signatures.append(_pytest_signature(output))
    rc, output = _run([sys.executable, "-m", "pytest", "-q", "-p", "randomly"], timeout=600)
    if rc != 0:
        return _bad(
            "T4",
            "suite is not order/run dependent",
            f"random-order rc={rc}",
            [output[-400:]],
            mode="full",
        )
    signatures.append(_pytest_signature(output))
    if len(set(signatures)) != 1:
        return _bad(
            "T4", "suite is not order/run dependent", f"varying results: {signatures}", mode="full"
        )
    return _ok(
        "T4", "suite is not order/run dependent", f"4 runs stable: {signatures[0]}", mode="full"
    )


def check_t7_no_network(_: dict) -> CheckResult:
    if not _module_available("pytest_socket"):
        return _blocked("T7", "no network in unit tests", "pytest-socket not installed")
    rc, output = _run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "--disable-socket"], timeout=600
    )
    if rc != 0:
        return _bad("T7", "no network in unit tests", f"rc={rc}", [output[-500:]], mode="full")
    return _ok("T7", "no network in unit tests", "all tests pass offline", mode="full")


def check_t5_mutation(_: dict) -> CheckResult:
    if sys.platform == "win32":
        return _blocked(
            "T5",
            "mutation score (evaluator/verifier/ranker/filters)",
            "mutmut does not support native Windows (requires WSL)",
        )
    return _blocked("T5", "mutation score", "mutation tool not available in this environment")


def check_t6_vulture(_: dict) -> CheckResult:
    if not _module_available("vulture"):
        return _blocked("T6", "no unexplained dead code (vulture)", "vulture not installed")
    rc, output = _run(
        [sys.executable, "-m", "vulture", "atlas", "--min-confidence", "80"], timeout=300
    )
    findings = [line for line in output.splitlines() if line.strip()]
    if rc != 0 and findings:
        return _bad(
            "T6",
            "no unexplained dead code (vulture)",
            f"{len(findings)} findings",
            findings,
            mode="full",
        )
    return _ok("T6", "no unexplained dead code (vulture)", "no findings", mode="full")


FULL_CHECKS: list[Callable[[dict], CheckResult]] = [
    check_h3_history_secrets,
    check_t1_tests,
    check_t2_coverage,
    check_t3_lint_format_types,
    check_t4_flaky,
    check_t5_mutation,
    check_t6_vulture,
    check_t7_no_network,
]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def run_checks(
    mode: str = "full",
    *,
    checks: list[Callable[[dict], CheckResult]] | None = None,
    git_sha: str | None = None,
) -> SelfCheckReport:
    settings = get_settings()
    context = {"settings": settings, "mode": mode}
    if checks is None:
        checks = list(FAST_CHECKS)
        if mode == "full":
            checks += list(FULL_CHECKS)
    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(check(context))
        except Exception as exc:  # noqa: BLE001 - a crashing check is a FAIL, not a crash
            results.append(
                CheckResult(check.__name__, check.__name__, FAIL, f"check raised: {exc!r}", mode)
            )
    return SelfCheckReport(
        generated_at=datetime.now(UTC).isoformat(),
        mode=mode,
        git_sha=git_sha or _git_sha(),
        config_hash=_config_hash(),
        prompt_versions=_prompt_versions(),
        models={
            "extractor": settings.models.extractor,
            "evaluator": settings.models.evaluator,
            "verifier": settings.models.verifier,
        },
        checks=results,
    )


def format_report(report: SelfCheckReport) -> str:
    lines = [
        (
            f"Atlas self-check ({report.mode})  sha={report.git_sha[:12]}  "
            f"config={report.config_hash}"
        ),
    ]
    for check in report.checks:
        lines.append(f"{check.id:>4} | {check.name:<44} | {check.status:<7} | {check.evidence}")
    counts = report.summary()
    lines.append(
        f"---- {counts[PASS]} PASS / {counts[FAIL]} FAIL / {counts[BLOCKED]} BLOCKED / "
        f"{counts[SKIPPED]} SKIPPED  => exit {report.exit_code}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, print_fn=print) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="atlas selfcheck", description="Pre-Phase-8 audit checks")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fast", action="store_true", help="in-process checks only (default)")
    group.add_argument("--full", action="store_true", help="also run external toolchain checks")
    parser.add_argument("--report", default="selfcheck_report.json", help="JSON report output path")
    args = parser.parse_args(argv)

    mode = "full" if args.full else "fast"
    report = run_checks(mode)
    print_fn(format_report(report))
    Path(args.report).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return report.exit_code
