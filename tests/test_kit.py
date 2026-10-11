"""Application-kit generator tests (fake LLM, no network).

Every claim in the kit must carry a verbatim quote from the profile or the job
posting; a claim whose quote does not validate is dropped and its section is
flagged "needs your input".
"""

from __future__ import annotations

import json

from fake_llm import FakeClient

from atlas.kit import generate_kit
from atlas.llm import LLMClient
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
    {"text": "Acme needs a Data engineer.", "quote": "hiring a Data engineer", "source": "jd"},
    {
        "text": "The role builds pipelines in Python.",
        "quote": "build pipelines in Python",
        "source": "jd",
    },
    {
        "text": "Ada built a job pipeline project.",
        "quote": "Builds a job pipeline",
        "source": "profile",
    },
]

VALID_COVER = [
    {"text": "I have built a job pipeline.", "quote": "Builds a job pipeline", "source": "profile"}
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
            },
            {
                "question_key": "links",
                "answer": "My GitHub is https://github.com/ada.",
                "quote": "github: https://github.com/ada",
                "source": "profile",
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
    assert kit.prompt_version == "2"
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
            },
            {
                "question_key": "why_company",
                "answer": "Acme is hiring a Data engineer.",
                "quote": "hiring a Data engineer",
                "source": "jd",
            },
        ],
    }
    kit = generate_kit(_client(payload), profile=_profile(), job=_job())

    keys = {answer.question_key for answer in kit.answers}
    assert "availability" not in keys
    assert "why_company" in keys
    kept = kit.answers[0]
    assert kept.quote and kept.source.value in {"jd", "profile"}
