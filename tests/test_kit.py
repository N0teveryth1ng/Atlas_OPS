"""Application-kit generator tests (fake LLM, no network).

Every claim in the kit must carry a verbatim quote from the profile or the job
posting; a claim whose quote does not validate is dropped and its section is
flagged "needs your input".
"""

from __future__ import annotations

import json

from fake_llm import FakeClient

from atlas.kit import ClaimKind, KitClaim, generate_kit, word_count
from atlas.llm import LLMClient
from atlas.prompt_store import load_prompt
from atlas.schemas import (
    CandidateProfile,
    Experience,
    InputMode,
    Job,
    Links,
    Project,
    Skill,
    SkillCategory,
)

VALID_SUMMARY = [
    {
        "text": "Acme needs a Data engineer.",
        "quote": "hiring a Data engineer",
        "source": "jd",
        "kind": "about_job",
    },
    {
        "text": "The role builds pipelines in Python.",
        "quote": "build pipelines in Python",
        "source": "jd",
        "kind": "about_job",
    },
    {
        "text": "Ada built a job pipeline project.",
        "quote": "Builds a job pipeline",
        "source": "profile",
        "kind": "about_candidate",
    },
]

VALID_COVER = [
    {
        "text": "I have built a job pipeline.",
        "quote": "Builds a job pipeline",
        "source": "profile",
        "kind": "about_candidate",
    }
]


def _client(payload: dict) -> LLMClient:
    """A fake Groq-like client that always returns ``payload`` and never talks
    to the network."""
    return LLMClient(client=FakeClient(lambda kwargs: json.dumps(payload)))


def _profile() -> CandidateProfile:
    return CandidateProfile(
        source_mode=InputMode.resume,
        full_name="Ada Lovelace",
        links=Links(github="https://github.com/ada", linkedin="https://linkedin.com/in/ada"),
        total_experience_months=30,
        experience_level="mid",
        skills=[
            Skill(name="python", canonical_name="python", category=SkillCategory.language),
        ],
        projects=[
            Project(
                title="Atlas Ops",
                tech=["python"],
                summary="Builds a job pipeline",
                outcomes="live",
            )
        ],
        work=[Experience(role="Data engineer", company="Acme")],
        target_roles=["Backend"],
    )


def _job() -> Job:
    return Job(
        source="test",
        url="https://acme.com/jobs/1",
        dedupe_key="acme-1",
        title="Data engineer",
        company="Acme",
        location="Remote",
        description_raw="We are hiring a Data engineer to build pipelines in Python.",
    )


def test_generate_kit_keeps_valid_quotes_and_records_prompt_version():
    payload = {
        "summary": VALID_SUMMARY,
        "cover_note": VALID_COVER,
        "answers": [
            {
                "question_key": "why_company",
                "answer": "Acme is hiring a Data engineer.",
                "quote": "hiring a Data engineer",
                "source": "jd",
                "kind": "about_job",
            },
            {
                "question_key": "links",
                "answer": "My GitHub is https://github.com/ada.",
                "quote": "github: https://github.com/ada",
                "source": "profile",
                "kind": "about_candidate",
            },
        ],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    assert [claim.text for claim in kit.summary.claims] == [c["text"] for c in VALID_SUMMARY]
    assert kit.summary.dropped == 0
    assert kit.summary.blocked_reason is None
    assert [claim.text for claim in kit.cover_note.claims] == [c["text"] for c in VALID_COVER]
    assert kit.cover_note.blocked_reason is None
    assert {answer.question_key for answer in kit.answers} == {"why_company", "links"}
    assert kit.prompt_version == "4"
    assert kit.model


def test_invalid_quote_drops_claim_and_flags_section():
    payload = {
        "summary": [
            VALID_SUMMARY[0],
            VALID_SUMMARY[1],
            {
                "text": "Ada has a PhD in machine learning.",
                "quote": "PhD in machine learning",
                "source": "profile",
            },
        ],
        "cover_note": VALID_COVER,
        "answers": [],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    kept = [claim.text for claim in kit.summary.claims]
    assert "Ada has a PhD in machine learning." not in kept
    assert len(kit.summary.claims) == 2
    assert kit.summary.dropped == 1
    assert kit.summary.blocked_reason == "needs your input"


def test_all_claims_invalid_blocks_section():
    bad = {"text": "Ada has a PhD in machine learning.", "quote": "PhD", "source": "profile"}
    payload = {"summary": [bad, bad, bad], "cover_note": [bad], "answers": []}
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    assert kit.summary.claims == []
    assert kit.summary.dropped == 3
    assert kit.summary.blocked_reason == "needs your input"
    assert kit.cover_note.claims == []
    assert kit.cover_note.blocked_reason == "needs your input"


def test_prompt_uses_the_jd_source_token():
    """The evidence enum value is "jd"; the prompt must not ask for "job"."""
    text, _version = load_prompt("application_kit")
    assert '"jd"' in text
    assert '"job"' not in text


def test_summary_beyond_three_claims_is_truncated_not_just_flagged():
    payload = {
        "summary": [
            *VALID_SUMMARY,
            {
                "text": "Acme is hiring a Data engineer now.",
                "quote": "hiring a Data engineer",
                "source": "jd",
                "kind": "about_job",
            },
        ],
        "cover_note": VALID_COVER,
        "answers": [],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    assert len(kit.summary.claims) == 3
    assert kit.summary.dropped == 1
    assert kit.summary.blocked_reason == "needs your input"


def test_cover_note_beyond_word_bound_is_truncated_not_just_flagged():
    long_text = "word " * 60  # 60 words per claim
    claim = {"text": long_text, "quote": "Builds a job pipeline", "source": "profile"}
    payload = {
        "summary": VALID_SUMMARY,
        "cover_note": [dict(claim) for _ in range(3)],
        "answers": [],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    total = sum(word_count(c.text) for c in kit.cover_note.claims)
    assert total < 150
    assert kit.cover_note.dropped >= 1
    assert kit.cover_note.blocked_reason == "needs your input"


def test_answer_without_valid_quote_is_dropped():
    payload = {
        "summary": VALID_SUMMARY,
        "cover_note": VALID_COVER,
        "answers": [
            {
                "question_key": "availability",
                "answer": "I can start immediately with no notice.",
                "quote": "available immediately",
                "source": "profile",
                "kind": "about_candidate",
            },
            {
                "question_key": "why_company",
                "answer": "Acme is hiring a Data engineer.",
                "quote": "hiring a Data engineer",
                "source": "jd",
                "kind": "about_job",
            },
        ],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    keys = {answer.question_key for answer in kit.answers}
    assert "availability" not in keys
    assert "why_company" in keys
    kept = kit.answers[0]
    assert kept.quote and kept.source.value in {"jd", "profile"}


def _job_with_requirements() -> Job:
    return Job(
        source="test",
        url="https://acme.com/jobs/1",
        dedupe_key="acme-1",
        title="Data engineer",
        company="Acme",
        location="Remote",
        description_raw=(
            "We are hiring a Data engineer to build pipelines in Python. "
            "3+ years of Python required."
        ),
    )


def test_kit_claim_without_kind_defaults_to_about_candidate():
    claim = KitClaim(text="x", quote="y", source="jd")
    assert claim.kind == ClaimKind.about_candidate


def test_candidate_claim_quoting_only_the_jd_is_dropped():
    """A claim about the candidate whose quote is only in the JD is not grounded."""
    payload = {
        "summary": [
            VALID_SUMMARY[0],
            VALID_SUMMARY[1],
            {
                "text": "I have 3 years of Python.",
                "quote": "3+ years of Python required",
                "source": "jd",
                "kind": "about_candidate",
            },
        ],
        "cover_note": VALID_COVER,
        "answers": [],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job_with_requirements())

    kept = [claim.text for claim in kit.summary.claims]
    assert "I have 3 years of Python." not in kept
    assert len(kit.summary.claims) == 2
    assert kit.summary.dropped == 1
    assert kit.summary.blocked_reason == "needs your input"


def test_claim_without_kind_quoting_the_jd_is_dropped():
    """A missing kind is strict (about_candidate), so a JD-only quote is dropped."""
    payload = {
        "summary": [
            VALID_SUMMARY[0],
            VALID_SUMMARY[1],
            {
                "text": "I have 3 years of Python.",
                "quote": "3+ years of Python required",
                "source": "jd",
            },
        ],
        "cover_note": VALID_COVER,
        "answers": [],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job_with_requirements())

    kept = [claim.text for claim in kit.summary.claims]
    assert "I have 3 years of Python." not in kept
    assert kit.summary.dropped == 1
    assert kit.summary.blocked_reason == "needs your input"


def test_job_claim_quoting_the_jd_is_kept():
    payload = {
        "summary": [VALID_SUMMARY[0], VALID_SUMMARY[1], VALID_SUMMARY[2]],
        "cover_note": VALID_COVER,
        "answers": [],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    kept = [claim.text for claim in kit.summary.claims]
    assert "Acme needs a Data engineer." in kept
    assert kit.summary.dropped == 0
    assert kit.summary.blocked_reason is None


def test_candidate_claim_quoting_the_profile_is_kept():
    candidate = {
        "text": "Ada built a job pipeline project.",
        "quote": "Builds a job pipeline",
        "source": "profile",
        "kind": "about_candidate",
    }
    payload = {
        "summary": [VALID_SUMMARY[0], VALID_SUMMARY[1], candidate],
        "cover_note": VALID_COVER,
        "answers": [],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    kept = [claim.text for claim in kit.summary.claims]
    assert candidate["text"] in kept
    assert kit.summary.blocked_reason is None
