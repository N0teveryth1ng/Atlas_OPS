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
from .db import connect, get_latest_profile, init_db, save_profile
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
    print("`run` is implemented in Phase 4+. Not available yet.", file=sys.stderr)
    return 1


def cmd_eval(args: argparse.Namespace) -> int:
    print("`eval` is implemented in Phase 2+. Not available yet.", file=sys.stderr)
    return 1


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

    p_run = sub.add_parser("run", help="Run the pipeline (later phases)")
    p_run.set_defaults(func=cmd_run)

    p_eval = sub.add_parser("eval", help="Run the golden-set evaluation (later phases)")
    p_eval.set_defaults(func=cmd_eval)

    p_fb = sub.add_parser("feedback", help="Record feedback (later phases)")
    p_fb.set_defaults(func=cmd_feedback)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
