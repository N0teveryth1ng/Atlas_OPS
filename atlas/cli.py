"""Atlas_OPS command-line interface.

Usage::

    python -m atlas.cli profile --resume resume.pdf --describe "..." --out profile.json
    python -m atlas.cli review profile.json
    python -m atlas.cli run            # full pipeline + digest (never emails)
    python -m atlas.cli dashboard      # local matched-jobs dashboard (APLD/NTAPLD/PEND)
    python -m atlas.cli schedule       # run now, then daily
    python -m atlas.cli decide       # run the decision engine over stored jobs
    python -m atlas.cli explain <job_id>
    python -m atlas.cli decide           # run the decision engine over stored jobs
    python -m atlas.cli explain <job_id>  # print the latest decision for a job
    python -m atlas.cli eval             # golden-set evaluation
    python -m atlas.cli feedback <job_id> good|bad --reason <code>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, get_settings
from .db import (
    connect,
    finish_run,
    get_latest_profile,
    init_db,
    save_profile,
    start_run,
)
from .llm import LLMClient
from .logging_setup import setup_logging
from .profile_agent import build_profile, render_profile_summary
from .schemas import CandidateProfile

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_PATH = REPO_ROOT / "profile.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_client() -> LLMClient:
    settings = get_settings()
    return LLMClient(
        api_key=settings.secrets.groq_api_key,
        default_model=settings.models.extractor,
        temperature=settings.models.temperature,
    )


def _read_description(args: argparse.Namespace) -> str | None:
    if args.describe:
        return args.describe
    if args.describe_file:
        return Path(args.describe_file).read_text(encoding="utf-8")
    return None


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return ""


def _resolve_missing_fields(profile: CandidateProfile) -> None:
    """Ask the human for critical fields instead of guessing."""
    missing = profile.missing_critical_fields()
    if not missing:
        return

    print("\nSome required details are missing. Please provide them (blank = skip):")
    if "email" in missing:
        value = _prompt("  Email: ")
        if value:
            profile.email = value
    if "target_roles" in missing:
        value = _prompt("  Target roles (comma-separated): ")
        if value:
            profile.target_roles = [r.strip() for r in value.split(",") if r.strip()]
    if "skills" in missing:
        value = _prompt("  Skills (comma-separated): ")
        if value:
            from .profile_agent import normalize_skill_name
            from .schemas import Skill

            profile.skills = [
                Skill(name=s, canonical_name=normalize_skill_name(s))
                for s in (x.strip() for x in value.split(","))
                if s
            ]


def _review(profile: CandidateProfile, *, auto_approve: bool) -> CandidateProfile:
    print("\n" + "=" * 60)
    print(render_profile_summary(profile))
    print("=" * 60)

    if auto_approve:
        profile.approved = True
        return profile

    _resolve_missing_fields(profile)

    if profile.ambiguities:
        print("\nUnresolved ambiguities (edit profile.json manually if needed):")
        for item in profile.ambiguities:
            print(f"  - {item}")

    answer = _prompt("\nApprove this profile for use? [y/N] ").lower()
    profile.approved = answer in {"y", "yes"}
    if not profile.approved:
        print("Profile not approved. Edit profile.json and run `review` again.")
    return profile


def _write_profile(profile: CandidateProfile, path: Path) -> None:
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote {path}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_profile(args: argparse.Namespace) -> int:
    description = _read_description(args)
    if not args.resume and not description:
        print("Provide --resume, --describe, or --describe-file.", file=sys.stderr)
        return 2

    client = _make_client()
    resume_source = Path(args.resume) if args.resume else None
    profile = build_profile(client, resume_source=resume_source, description=description)

    out_path = Path(args.out)
    _write_profile(profile, out_path)
    profile = _review(profile, auto_approve=args.yes)
    _write_profile(profile, out_path)

    if profile.approved:
        conn = connect()
        init_db(conn)
        save_profile(conn, profile)
        conn.close()
        print("Profile approved and saved to the database.")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"Not found: {path}", file=sys.stderr)
        return 2
    profile = CandidateProfile.model_validate_json(path.read_text(encoding="utf-8"))
    profile = _review(profile, auto_approve=args.yes)
    _write_profile(profile, path)

    if profile.approved:
        conn = connect()
        init_db(conn)
        save_profile(conn, profile)
        conn.close()
        print("Profile approved and saved to the database.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return _execute_run(args, run_kind="run")


def _execute_run(args: argparse.Namespace, *, run_kind: str = "run") -> int:
    from .db import emailed_job_ids, mark_digest_sent, save_digest
    from .digest import build_digest, render_html, render_text
    from .feedback import load_and_apply
    from .pipeline import run_pipeline

    settings = get_settings()
    conn = connect()
    init_db(conn)
    profile = get_latest_profile(conn)
    if profile is None:
        print("No saved profile. Run `profile` first.", file=sys.stderr)
        conn.close()
        return 2

    settings, tuning = load_and_apply(conn, settings)
    if tuning.feedback_count:
        print(f"Tuning: {tuning.summary()}")

    client = _make_client()
    run_id = start_run(conn, run_kind)
    sourcing_result = None
    if getattr(args, "collect", False):
        from .sourcing import run_sourcing

        sourcing_result = run_sourcing(profile, settings=settings, conn=conn, run_id=run_id)

    results = run_pipeline(
        conn, run_id, profile, settings, client, limit=getattr(args, "limit", None)
    )
    from .job_status import InvariantViolation, JobStatus, assert_sent_subset

    needs_review = [r for r in results if r.status == JobStatus.needs_review]
    if needs_review:
        print(f"{len(needs_review)} job(s) need review and were not ranked or shipped.")
    shippable_results = [r for r in results if r.status != JobStatus.needs_review]

    already_sent = emailed_job_ids(conn)
    digest = build_digest(shippable_results, settings, run_id=run_id, already_sent=already_sent)
    digest.summary.quote_validation_failures = sum(1 for r in results if r.evidence_failed)
    print(render_text(digest))

    passed_ids = {
        r.job_id for r in results if r.filter_result is not None and r.filter_result.passed
    }
    try:
        assert_sent_subset(passed_ids, set(digest.job_ids()))
    except InvariantViolation as exc:
        logger.error("shipping invariant violated, aborting digest: %s", exc)
        digest = build_digest([], settings, run_id=run_id)

    digest_id = save_digest(
        conn, run_id, settings.filters.top_k, render_html(digest), render_text(digest)
    )
    # Email delivery was cancelled: the digest is only printed and served by
    # the dashboard, so it counts as published the moment it is persisted.
    mark_digest_sent(conn, digest_id)
    if sourcing_result is not None:
        from .sourcing import apply_pipeline_yields

        # Nothing is emailed any more, so a source's `sent` column must stay 0
        # even though the digest ids are in it.
        apply_pipeline_yields(sourcing_result, results, set())
        for item in sourcing_result.source_yields:
            print(
                f"  {item.source:<15} fetched={item.fetched:<5} kept={item.kept:<5} "
                f"passed={item.passed_filters:<5} sent={item.sent}"
            )
    finish_run(conn, run_id, "ok", summary=digest.summary.model_dump())
    conn.close()
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """Run the decision engine over stored jobs (dry-run; no email)."""
    from .pipeline import run_pipeline

    settings = get_settings()
    conn = connect()
    init_db(conn)
    profile = get_latest_profile(conn)
    if profile is None:
        print("No saved profile. Run `profile` first.", file=sys.stderr)
        conn.close()
        return 2

    client = _make_client()
    run_id = start_run(conn, "decide")
    results = run_pipeline(
        conn, run_id, profile, settings, client, limit=getattr(args, "limit", None)
    )

    for result in results:
        if result.decision is None:
            continue
        print(
            f"- [{result.decision.outcome.value.upper()}] "
            f"score={result.decision.match_score:.0f} "
            f"conf={result.decision.confidence:.2f} "
            f"{(result.job.title or '')} @ {result.job.company or ''}"
        )
        for veto in result.decision.vetoes:
            tag = "HARD" if veto.hard else "SOFT"
            print(f"    ! {tag} {veto.rule_id}: {veto.reason}")

    finish_run(conn, run_id, "ok", {})
    conn.close()
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Print the latest decision record for a single job."""
    from .db import get_latest_decision

    conn = connect()
    init_db(conn)
    decision = get_latest_decision(conn, args.job_id)
    conn.close()
    if decision is None:
        print(f"No decision recorded for job {args.job_id}.")
        return 0
    print(json.dumps(decision, indent=2))
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from .scheduler import serve

    settings = get_settings()
    hour = args.hour if args.hour is not None else settings.schedule.daily_hour

    run_args = argparse.Namespace(collect=not args.no_collect, limit=args.limit)

    def run_once() -> None:
        code = _execute_run(run_args, run_kind="scheduled")
        if code:
            logger.warning("Scheduled run exited with code %s", code)

    print(f"Scheduling daily run at {hour:02d}:00 local time (Ctrl+C to stop).")
    serve(run_once, daily_hour=hour, run_immediately=not args.no_immediate)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from .dashboard import create_app

    print(f"Dashboard: http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from .sourcing import run_sourcing

    settings = get_settings()
    conn = connect()
    init_db(conn)
    profile = get_latest_profile(conn)
    if profile is None:
        print("No saved profile. Run `profile` first.", file=sys.stderr)
        conn.close()
        return 2

    run_id = start_run(conn, "collect")
    result = run_sourcing(profile, settings=settings, conn=conn, run_id=run_id)
    finish_run(
        conn,
        run_id,
        "ok",
        summary={
            "queries": len(result.queries),
            "jobs": len(result.jobs),
            "new_jobs": result.new_jobs,
            "dropped_stale": result.dropped_stale,
        },
    )
    conn.close()

    print(
        f"Queries: {len(result.queries)}   Collected: {len(result.jobs)}   "
        f"New: {result.new_jobs}   Stale dropped: {result.dropped_stale}"
    )
    for item in result.source_yields:
        # cmd_collect never runs the pipeline, so passed/sent stay 0 here.
        print(
            f"  {item.source:<15} fetched={item.fetched:<5} kept={item.kept:<5} "
            f"passed={item.passed_filters:<5} sent={item.sent}"
        )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .db import record_eval_history
    from .evaluation import run_eval, run_llm_eval, run_skill_eval

    print("== JD parse + hard filter ==")
    golden_ok = run_eval(Path(args.golden)) if args.golden else run_eval()
    print("\n== Skill matching ==")
    skill_ok = run_skill_eval()
    ok = golden_ok and skill_ok

    llm_ok: bool | None = None
    if args.with_llm:
        print("\n== LLM precision@10 (live model) ==")
        llm_ok = run_llm_eval()
        ok = ok and llm_ok

    conn = connect()
    init_db(conn)
    record_eval_history(conn, "golden", golden_ok, {})
    record_eval_history(conn, "skill", skill_ok, {})
    if llm_ok is not None:
        record_eval_history(conn, "llm_precision_at_10", llm_ok, {})
    conn.close()
    return 0 if ok else 1


def cmd_feedback(args: argparse.Namespace) -> int:
    from .db import load_feedback
    from .feedback import export_golden, record_feedback

    conn = connect()
    init_db(conn)

    if args.export:
        count = export_golden(conn)
        print(f"Exported {count} new feedback case(s) to eval/feedback.jsonl.")
        conn.close()
        return 0

    if args.job_id is None or args.verdict is None:
        print(
            "Usage: atlas feedback <job_id> good|bad [--reason <code>] [--note ...]",
            file=sys.stderr,
        )
        conn.close()
        return 2

    try:
        feedback_id = record_feedback(
            conn, args.job_id, args.verdict, reason_code=args.reason, note=args.note
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        conn.close()
        return 2

    total = len(load_feedback(conn))
    print(
        f"Recorded feedback #{feedback_id} for job {args.job_id} ({args.verdict}). ({total} total)"
    )
    conn.close()
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    from .selfcheck import main as selfcheck_main

    argv: list[str] = []
    if args.full:
        argv.append("--full")
    else:
        argv.append("--fast")
    argv += ["--report", args.report]
    return selfcheck_main(argv)


def cmd_status(args: argparse.Namespace) -> int:
    conn = connect()
    init_db(conn)
    profile = get_latest_profile(conn)
    conn.close()
    if profile is None:
        print("No saved profile.")
        return 0
    print(
        f"Latest profile: {profile.experience_level.value}, "
        f"{len(profile.skills)} skills, approved={profile.approved}"
    )
    return 0


def cmd_label_check(args: argparse.Namespace) -> int:
    from .labeling import format_report, validate_labels

    report = validate_labels(args.labels, pool_path=args.pool)
    print(format_report(report))
    return 0 if report.meets_minimums else 1


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    from .feedback import REASON_CODES

    parser = argparse.ArgumentParser(prog="atlas", description="Atlas_OPS job-matching pipeline")
    parser.add_argument("--version", action="version", version=f"atlas {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_profile = sub.add_parser("profile", help="Build a profile from a resume and/or description")
    p_profile.add_argument("--resume", help="Path to a resume PDF")
    p_profile.add_argument("--describe", help="Free-text self-description")
    p_profile.add_argument("--describe-file", help="Path to a text file with the description")
    p_profile.add_argument(
        "--out", default=str(DEFAULT_PROFILE_PATH), help="Output profile.json path"
    )
    p_profile.add_argument(
        "--yes", action="store_true", help="Skip interactive review (auto-approve)"
    )
    p_profile.set_defaults(func=cmd_profile)

    p_review = sub.add_parser("review", help="Review and approve an existing profile.json")
    p_review.add_argument("path", nargs="?", default=str(DEFAULT_PROFILE_PATH))
    p_review.add_argument("--yes", action="store_true", help="Auto-approve")
    p_review.set_defaults(func=cmd_review)

    sub.add_parser("status", help="Show the latest saved profile").set_defaults(func=cmd_status)

    p_run = sub.add_parser("run", help="Run the pipeline")
    p_run.add_argument("--collect", action="store_true", help="Fetch new jobs first (Phase 4)")
    p_run.add_argument("--limit", type=int, default=None, help="Process at most N stored jobs")
    p_run.set_defaults(func=cmd_run)

    p_decide = sub.add_parser(
        "decide", help="Run the decision engine over stored jobs (dry-run, no email)"
    )
    p_decide.add_argument("--limit", type=int, default=None, help="Process at most N stored jobs")
    p_decide.set_defaults(func=cmd_decide)

    p_explain = sub.add_parser("explain", help="Print the latest decision for a job")
    p_explain.add_argument("job_id", type=int, help="Job id from the digest/DB")
    p_explain.set_defaults(func=cmd_explain)

    p_labels = sub.add_parser(
        "label-check", help="Validate a human-labelled labels file (read-only)"
    )
    p_labels.add_argument(
        "--labels", default="eval/labels.jsonl", help="Path to the labels JSONL file"
    )
    p_labels.add_argument(
        "--pool", default="eval/labeling_pool.jsonl", help="Path to the labeling pool JSONL file"
    )
    p_labels.set_defaults(func=cmd_label_check)

    p_sched = sub.add_parser("schedule", help="Run once now, then daily at the configured hour")
    p_sched.add_argument("--hour", type=int, default=None, help="Local hour 0-23 (default: config)")
    p_sched.add_argument("--no-collect", action="store_true", help="Do not fetch new jobs")
    p_sched.add_argument("--limit", type=int, default=None, help="Process at most N stored jobs")
    p_sched.add_argument(
        "--no-immediate", dest="immediate", action="store_false", help="Wait until the next hour"
    )
    p_sched.set_defaults(func=cmd_schedule, immediate=True)

    p_dash = sub.add_parser("dashboard", help="Serve the matched-jobs dashboard (local)")
    p_dash.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p_dash.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    p_dash.set_defaults(func=cmd_dashboard)

    sub.add_parser("collect", help="Fetch + normalize + dedupe jobs (Phase 4)").set_defaults(
        func=cmd_collect
    )

    p_eval = sub.add_parser("eval", help="Run the golden-set evaluation (Phase 2)")
    p_eval.add_argument(
        "--golden", help="Path to a golden-set .jsonl (default: eval/golden_set.jsonl)"
    )
    p_eval.add_argument(
        "--with-llm", action="store_true", help="Also run the live LLM precision@10 gate"
    )
    p_eval.set_defaults(func=cmd_eval)

    p_self = sub.add_parser("selfcheck", help="Run the pre-Phase-8 audit checks")
    p_self.add_argument("--fast", action="store_true", help="In-process checks only")
    p_self.add_argument(
        "--full", action="store_true", help="Also run the external toolchain checks"
    )
    p_self.add_argument("--report", default="selfcheck_report.json", help="JSON report output path")
    p_self.set_defaults(func=cmd_selfcheck, full=False)

    p_fb = sub.add_parser("feedback", help="Record feedback and tune future runs")
    p_fb.add_argument("job_id", nargs="?", type=int, help="Job id from the digest/DB")
    p_fb.add_argument("verdict", nargs="?", choices=["good", "bad"], help="Your judgement")
    p_fb.add_argument("--reason", choices=REASON_CODES, help="Reason code (for 'bad', etc.)")
    p_fb.add_argument("--note", help="Optional free-text note")
    p_fb.add_argument(
        "--export", action="store_true", help="Append feedback to eval/feedback.jsonl"
    )
    p_fb.set_defaults(func=cmd_feedback)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")  # never crash on odd job titles
            except (AttributeError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    setup_logging()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
