"""Atlas_OPS command-line interface.

Usage::

    python -m atlas.cli profile --resume resume.pdf --describe "..." --out profile.json
    python -m atlas.cli review profile.json
    python -m atlas.cli run        # later phases
    python -m atlas.cli eval       # later phases
    python -m atlas.cli feedback   # later phases
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, get_settings
from .db import connect, finish_run, get_latest_profile, init_db, save_profile, start_run
from .llm import LLMClient
from .logging_setup import setup_logging
from .profile_agent import build_profile, render_profile_summary
from .schemas import CandidateProfile, ExperienceLevel

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
    from .db import emailed_job_ids, mark_digest_sent, mark_jobs_emailed, save_digest
    from .digest import build_digest, render_html, render_text
    from .emailer import EmailError, send_digest
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
    run_id = start_run(conn, run_kind)
    if getattr(args, "collect", False):
        from .sourcing import run_sourcing

        run_sourcing(profile, settings=settings, conn=conn, run_id=run_id)

    results = run_pipeline(
        conn, run_id, profile, settings, client, limit=getattr(args, "limit", None)
    )
    already_sent = emailed_job_ids(conn)
    digest = build_digest(results, settings, run_id=run_id, already_sent=already_sent)
    print(render_text(digest))

    sent = False
    if getattr(args, "email", False):
        if digest.is_empty():
            print("No new matches; skipping email.")
        else:
            try:
                send_digest(digest, settings)
                mark_jobs_emailed(conn, digest.job_ids())
                sent = True
                print(f"Emailed {digest.item_count} job(s).")
            except EmailError as exc:
                print(f"Email not sent: {exc}", file=sys.stderr)

    digest_id = save_digest(conn, run_id, settings.filters.top_k, render_html(digest), render_text(digest))
    if sent:
        mark_digest_sent(conn, digest_id)
    finish_run(conn, run_id, "ok", summary=digest.summary.model_dump())
    conn.close()
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from .scheduler import serve

    settings = get_settings()
    hour = args.hour if args.hour is not None else settings.schedule.daily_hour

    run_args = argparse.Namespace(
        collect=not args.no_collect, limit=args.limit, email=not args.no_email
    )

    def run_once() -> None:
        code = _execute_run(run_args, run_kind="scheduled")
        if code:
            logger.warning("Scheduled run exited with code %s", code)

    print(f"Scheduling daily run at {hour:02d}:00 local time (Ctrl+C to stop).")
    serve(run_once, daily_hour=hour, run_immediately=not args.no_immediate)
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

    print(f"Queries: {len(result.queries)}   Collected: {len(result.jobs)}   "
          f"New: {result.new_jobs}   Stale dropped: {result.dropped_stale}")
    for item in result.source_yields:
        print(f"  {item.source:<12} fetched={item.fetched:<5} kept={item.kept}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluation import run_eval, run_llm_eval, run_skill_eval

    print("== JD parse + hard filter ==")
    ok = run_eval(Path(args.golden)) if args.golden else run_eval()
    print("\n== Skill matching ==")
    ok = run_skill_eval() and ok
    if args.with_llm:
        print("\n== LLM precision@10 (live model) ==")
        ok = run_llm_eval() and ok
    return 0 if ok else 1


def cmd_feedback(args: argparse.Namespace) -> int:
    print("`feedback` is implemented in Phase 7. Not available yet.", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    conn = connect()
    init_db(conn)
    profile = get_latest_profile(conn)
    conn.close()
    if profile is None:
        print("No saved profile.")
        return 0
    print(f"Latest profile: {profile.experience_level.value}, "
          f"{len(profile.skills)} skills, approved={profile.approved}")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas", description="Atlas_OPS job-matching pipeline")
    parser.add_argument("--version", action="version", version=f"atlas {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_profile = sub.add_parser("profile", help="Build a profile from a resume and/or description")
    p_profile.add_argument("--resume", help="Path to a resume PDF")
    p_profile.add_argument("--describe", help="Free-text self-description")
    p_profile.add_argument("--describe-file", help="Path to a text file with the description")
    p_profile.add_argument("--out", default=str(DEFAULT_PROFILE_PATH), help="Output profile.json path")
    p_profile.add_argument("--yes", action="store_true", help="Skip interactive review (auto-approve)")
    p_profile.set_defaults(func=cmd_profile)

    p_review = sub.add_parser("review", help="Review and approve an existing profile.json")
    p_review.add_argument("path", nargs="?", default=str(DEFAULT_PROFILE_PATH))
    p_review.add_argument("--yes", action="store_true", help="Auto-approve")
    p_review.set_defaults(func=cmd_review)

    sub.add_parser("status", help="Show the latest saved profile").set_defaults(func=cmd_status)

    p_run = sub.add_parser("run", help="Run the pipeline")
    p_run.add_argument("--collect", action="store_true", help="Fetch new jobs first (Phase 4)")
    p_run.add_argument("--limit", type=int, default=None, help="Process at most N stored jobs")
    p_run.add_argument("--no-email", dest="email", action="store_false", help="Do not email the digest")
    p_run.set_defaults(func=cmd_run, email=True)

    p_sched = sub.add_parser("schedule", help="Run once now, then daily at the configured hour")
    p_sched.add_argument("--hour", type=int, default=None, help="Local hour 0-23 (default: config)")
    p_sched.add_argument("--no-collect", action="store_true", help="Do not fetch new jobs")
    p_sched.add_argument("--limit", type=int, default=None, help="Process at most N stored jobs")
    p_sched.add_argument("--no-email", dest="email", action="store_false", help="Do not email")
    p_sched.add_argument("--no-immediate", dest="immediate", action="store_false", help="Wait until the next hour")
    p_sched.set_defaults(func=cmd_schedule, email=True, immediate=True)

    sub.add_parser("collect", help="Fetch + normalize + dedupe jobs (Phase 4)").set_defaults(
        func=cmd_collect
    )

    p_eval = sub.add_parser("eval", help="Run the golden-set evaluation (Phase 2)")
    p_eval.add_argument("--golden", help="Path to a golden-set .jsonl (default: eval/golden_set.jsonl)")
    p_eval.add_argument("--with-llm", action="store_true", help="Also run the live LLM precision@10 gate")
    p_eval.set_defaults(func=cmd_eval)

    p_fb = sub.add_parser("feedback", help="Record feedback (later phases)")
    p_fb.set_defaults(func=cmd_feedback)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash on odd job titles
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    setup_logging()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
