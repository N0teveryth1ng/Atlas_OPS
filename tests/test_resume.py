"""Tailored-resume generator tests (fake LLM, no network).

Grounding is by profile *fact ids*: every summary sentence and bullet must cite at
least one real ``[ID]``. Only technologies, numbers and strength/ownership claims need
to be grounded — generic nouns (data, design, system, API, AI, service, platform,
pipeline) are ignored. Skills are assembled in code and evidence-linked. Requirements
are classified by ontology/alias/family first. A profile without internship/work/
project/link detail is refused rather than padded; a thin internship section warns.
"""

from __future__ import annotations

import json

import pytest
from fake_llm import FakeClient

from atlas.config import get_settings
from atlas.db import (
    add_llm_tokens,
    approve_tailored_resume,
    approved_tailored_resume,
    connect,
    get_tailored_resume,
    init_db,
    latest_tailored_resume,
    save_tailored_resume,
    start_run,
    tokens_used_today,
    upsert_job,
)
from atlas.llm import LLMClient
from atlas.pipeline import LLMCallBudgetExceeded
from atlas.resume import (
    MAX_MAIN_SKILLS,
    MAX_RESUME_CALLS,
    PROFILE_VERBATIM,
    ResumeBlock,
    _tokenize,
    ats_lint,
    build_facts,
    build_skills,
    classify_skills,
    format_dates,
    generate_tailored_resume,
    generate_tailored_resume_cached,
    generate_tailored_resume_under_caps,
    profile_gaps,
    render_profile_facts,
    resume_blocks,
    skill_evidence_facts,
    strip_proficiency,
)
from atlas.schemas import (
    CandidateProfile,
    Education,
    Experience,
    InputMode,
    Job,
    Links,
    ParsedJD,
    Proficiency,
    Project,
    Skill,
    SkillCategory,
)
from atlas.skills import get_ontology
from atlas.text import clean_tech_items

VALID_SUMMARY = {"text": "Builds a job pipeline.", "facts": ["W1.s1"]}
VALID_EXPERIENCE_BULLET = {"text": "Build a job pipeline in Python.", "facts": ["W1.s1"]}
VALID_PROJECT_BULLET = {
    "text": "Shipped a live job pipeline.",
    "facts": ["P1.outcomes.s1", "P1.summary.s1"],
}
VALID_REQUIREMENT = {
    "requirement": "Python",
    "status": "covered",
    "facts": ["W1.s1"],
    "quote": "Builds a job pipeline",
}


def _client(payload: dict) -> LLMClient:
    """A fake Groq-like client that always returns ``payload``; no network."""
    return LLMClient(client=FakeClient(lambda kwargs: json.dumps(payload)))


def _profile() -> CandidateProfile:
    return CandidateProfile(
        source_mode=InputMode.resume,
        full_name="Ada Lovelace",
        email="ada@example.com",
        links=Links(github="https://github.com/ada"),
        total_experience_months=30,
        experience_level="mid",
        education=[
            Education(
                degree="BSc",
                field="Computer Science",
                institution="University of X",
                graduation_year=2020,
            )
        ],
        certifications=["AWS Certified"],
        skills=[
            Skill(
                name="Python",
                canonical_name="python",
                category=SkillCategory.language,
                proficiency=Proficiency.strong,
            ),
            Skill(
                name="React",
                canonical_name="react",
                category=SkillCategory.framework,
                proficiency=Proficiency.strong,
            ),
            Skill(
                name="Docker",
                canonical_name="docker",
                category=SkillCategory.tool,
                proficiency=Proficiency.working,
            ),
            Skill(
                name="PostgreSQL",
                canonical_name="postgresql",
                category=SkillCategory.database,
                proficiency=Proficiency.working,
            ),
            Skill(
                name="FastAPI",
                canonical_name="fastapi",
                category=SkillCategory.framework,
                proficiency=Proficiency.strong,
            ),
            Skill(
                name="Rust",
                canonical_name="rust",
                category=SkillCategory.language,
                proficiency=Proficiency.familiar,
            ),
        ],
        projects=[
            Project(
                title="Atlas Ops",
                tech=["Python", "FastAPI"],
                summary="Builds a job pipeline",
                outcomes="live",
            )
        ],
        work=[
            Experience(
                role="Data engineer",
                company="Acme",
                duration_months=30,
                tech=["Python"],
                description=["Builds a job pipeline"],
            )
        ],
        target_roles=["Backend"],
    )


def _job() -> Job:
    return Job(
        source="test",
        url="https://acme.com/jobs/1",
        dedupe_key="acme-1",
        title="Backend engineer",
        company="Acme",
        location="Remote",
        description_raw="We are hiring a Backend engineer who knows Python and ReactJS.",
    )


def _parsed_jd() -> ParsedJD:
    return ParsedJD(
        title="Backend engineer",
        must_have_skills=["Python", "Kubernetes"],
        nice_to_have_skills=["ReactJS"],
        confidence=0.9,
    )


def _payload(**overrides) -> dict:
    base = {
        "summary": [VALID_SUMMARY],
        "skills": ["Python (strong)"],
        "experience": [{"bullets": [VALID_EXPERIENCE_BULLET]}],
        "projects": [{"bullets": [VALID_PROJECT_BULLET]}],
        "requirements": [VALID_REQUIREMENT],
    }
    base.update(overrides)
    return base


def _generate(payload: dict, *, profile=None, job=None, parsed_jd=None):
    return generate_tailored_resume(
        _client(payload),
        profile=profile or _profile(),
        job=job or _job(),
        parsed_jd=parsed_jd or _parsed_jd(),
    )


# --------------------------------------------------------------------------- #
# Grounded lines + code-built skills
# --------------------------------------------------------------------------- #


def test_generate_keeps_valid_grounded_lines_and_records_prompt_version():
    resume = _generate(_payload())

    assert [line.text for line in resume.summary[1:]] == [VALID_SUMMARY["text"]]
    assert resume.summary[0].text.startswith("Mid backend developer")
    assert "Python" in resume.summary[0].text
    assert [b.text for b in resume.experience[0].bullets] == [VALID_EXPERIENCE_BULLET["text"]]
    assert [b.text for b in resume.projects[0].bullets] == [VALID_PROJECT_BULLET["text"]]
    assert resume.prompt_version == "4"
    assert resume.model
    assert resume.ats.passed is True


def test_skills_are_built_in_code_even_when_the_model_echoes_a_proficiency_suffix():
    resume = _generate(_payload(skills=["Python (strong)", "Kubernetes"]))

    assert "Python" in resume.skills
    assert "FastAPI" in resume.skills
    assert all("(" not in name for name in resume.skills + resume.familiar_skills)
    assert "Kubernetes" not in resume.skills
    assert "Kubernetes" not in resume.familiar_skills


def test_unevidenced_skill_goes_under_familiar():
    profile = _profile()
    profile.skills.append(
        Skill(
            name="Haskell",
            canonical_name="haskell",
            category=SkillCategory.language,
            proficiency=Proficiency.strong,
        )
    )
    skills, familiar = build_skills(profile, _parsed_jd(), get_ontology())

    assert "Haskell" in familiar
    assert "Haskell" not in skills


def test_confirmed_skill_with_no_evidence_stays_in_main_skills():
    profile = _profile()
    profile.skills.append(
        Skill(
            name="Haskell",
            canonical_name="haskell",
            category=SkillCategory.language,
            proficiency=Proficiency.strong,
            confirmed=True,
        )
    )
    skills, familiar = build_skills(profile, _parsed_jd(), get_ontology())

    assert "Haskell" in skills
    assert "Haskell" not in familiar


def test_familiar_skills_go_to_a_separate_group():
    resume = _generate(_payload())

    assert "Python" in resume.skills
    assert "FastAPI" in resume.skills
    assert "Rust" in resume.familiar_skills
    assert "Rust" not in resume.skills


def test_concept_skill_is_evidenced_by_project_outcomes_prose():
    profile = _profile()
    profile.projects[0].outcomes = "Implemented caching and circuit breakers"
    profile.skills.append(
        Skill(
            name="Caching",
            canonical_name="caching",
            category=SkillCategory.concept,
            proficiency=Proficiency.strong,
        )
    )

    assert skill_evidence_facts(profile, get_ontology())["Caching"] == "P1.outcomes.s1"
    skills, familiar = build_skills(profile, _parsed_jd(), get_ontology())
    assert "Caching" in skills
    assert "Caching" not in familiar


def test_tech_skill_evidence_cites_the_tech_fact_not_the_entry_title():
    profile = _profile()

    facts = skill_evidence_facts(profile, get_ontology())

    assert facts["FastAPI"] == "P1.tech"
    assert facts["Python"] == "P1.tech"
    assert all(not fact_id.endswith(".title") for fact_id in facts.values() if fact_id)


def test_rest_api_design_is_evidenced_by_apis_in_summary_prose():
    profile = _profile()
    profile.projects[0].summary = "Builds a job pipeline exposing REST APIs"
    profile.skills.append(
        Skill(
            name="REST API Design",
            canonical_name="rest",
            category=SkillCategory.concept,
            proficiency=Proficiency.strong,
        )
    )

    assert skill_evidence_facts(profile, get_ontology())["REST API Design"] == "P1.summary.s1"


def test_llm_pipeline_design_is_evidenced_by_internship_description_prose():
    profile = _profile()
    profile.work = []
    profile.internships = [
        Experience(
            role="Intern",
            company="GameGenesis",
            duration_months=3,
            tech=["Python"],
            description=["Integrated LLM pipelines for CRM automation"],
        )
    ]
    profile.skills.append(
        Skill(
            name="LLM pipeline design",
            canonical_name="llm pipeline design",
            category=SkillCategory.concept,
            proficiency=Proficiency.strong,
        )
    )

    facts = skill_evidence_facts(profile, get_ontology())

    assert facts["LLM pipeline design"] == "I1.s1"


def test_git_slash_github_and_plural_acronym_pass_grounding():
    text = "Built Git/GitHub workflows and REST APIs."
    profile = _profile()
    profile.work[0].description = [text]
    bullet = {"text": text, "facts": ["W1.s1"]}

    resume = _generate(_payload(experience=[{"bullets": [bullet]}]), profile=profile)

    assert [b.text for b in resume.experience[0].bullets] == [text]


def test_generic_ai_api_data_and_pipeline_do_not_cause_drops():
    profile = _profile()
    profile.work[0].description = ["Built REST APIs"]
    bullet = {
        "text": "Built REST APIs for AI data services and pipelines.",
        "facts": ["W1.s1"],
    }

    resume = _generate(_payload(experience=[{"bullets": [bullet]}]), profile=profile)

    assert [b.text for b in resume.experience[0].bullets] == [bullet["text"]]


def test_third_person_meta_sentence_is_rejected():
    meta = {"text": "The candidate has strong Python skills.", "facts": ["W1.s1"]}
    resume = _generate(_payload(summary=[meta]))

    assert len(resume.summary) == 1
    assert meta["text"] not in [line.text for line in resume.summary]
    assert any("meta" in line.reason for line in resume.dropped)


def test_led_claim_is_rejected_without_a_licensing_fact():
    claim = {"text": "Led backend development for a startup.", "facts": ["W1.s1"]}
    resume = _generate(_payload(summary=[claim]))

    assert claim["text"] not in [line.text for line in resume.summary]
    assert any("claim not supported" in line.reason for line in resume.dropped)


def test_ownership_claim_is_kept_when_the_fact_licenses_it():
    profile = _profile()
    profile.work[0].description = ["Owning the architecture and building services"]
    line = {"text": "Owning the architecture, building all services.", "facts": ["W1.s1"]}

    resume = _generate(_payload(experience=[{"bullets": [line]}]), profile=profile)

    assert [b.text for b in resume.experience[0].bullets] == [line["text"]]


@pytest.mark.parametrize(
    "phrase",
    [
        "production-grade",
        "from scratch",
        "core",
        "enterprise",
        "scalable",
        "end-to-end",
        "delivered",
    ],
)
def test_new_strength_terms_are_rejected_without_a_licensing_fact(phrase):
    line = {"text": f"Built {phrase} systems.", "facts": ["W1.s1"]}

    resume = _generate(_payload(experience=[{"bullets": [line]}]))

    assert resume.experience[0].bullets == []
    assert any(phrase in dropped.reason for dropped in resume.dropped)


def test_new_strength_term_is_kept_when_the_fact_licenses_it():
    profile = _profile()
    profile.work[0].description = ["Built core systems from scratch"]
    line = {"text": "Built core systems from scratch.", "facts": ["W1.s1"]}

    resume = _generate(_payload(experience=[{"bullets": [line]}]), profile=profile)

    assert [b.text for b in resume.experience[0].bullets] == [line["text"]]


def test_bullet_adding_a_technology_absent_from_the_cited_fact_is_dropped():
    bullet = {"text": "Deployed services to Kubernetes.", "facts": ["W1.s1"]}
    resume = _generate(_payload(experience=[{"bullets": [bullet]}]))

    assert resume.experience[0].bullets == []
    assert any("Kubernetes" in line.reason for line in resume.dropped)


def test_bullet_with_a_fabricated_metric_is_dropped():
    bullet = {"text": "Built a job pipeline handling 5000000 rows.", "facts": ["W1.s1"]}
    resume = _generate(_payload(experience=[{"bullets": [bullet]}]))

    assert resume.experience[0].bullets == []
    assert any("5000000" in line.reason for line in resume.dropped)


def test_bullet_without_a_cited_fact_is_dropped():
    bullet = {"text": "Built a job pipeline.", "facts": []}
    resume = _generate(_payload(experience=[{"bullets": [bullet]}]))

    assert resume.experience[0].bullets == []
    assert any("no fact id" in line.reason for line in resume.dropped)


def test_bullet_that_does_not_start_with_an_action_verb_is_dropped():
    bullet = {"text": "Responsible for a job pipeline.", "facts": ["W1.s1"]}
    resume = _generate(_payload(experience=[{"bullets": [bullet]}]))

    assert resume.experience[0].bullets == []
    assert any("action verb" in line.reason for line in resume.dropped)


def test_job_keyword_limit_drops_the_extras():
    profile = _profile()
    profile.work[0].description = ["Built REST services"]
    bullet = {"text": "Built REST services.", "facts": ["W1.s1"]}
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["Python"], nice_to_have_skills=["REST"]
    )
    resume = _generate(
        _payload(experience=[{"bullets": [bullet, bullet, bullet, bullet]}]),
        profile=profile,
        parsed_jd=parsed,
    )

    assert len(resume.experience[0].bullets) == 3
    assert any("keyword limit" in line.reason for line in resume.dropped)


# --------------------------------------------------------------------------- #
# Requirements
# --------------------------------------------------------------------------- #


def test_missing_requirements_are_classified_and_never_added():
    resume = _generate(_payload())

    assert "Kubernetes" in resume.missing_requirements
    assert "Python" not in resume.missing_requirements
    all_text = " ".join(line.text for line in resume.summary) + " ".join(
        b.text for entry in (*resume.experience, *resume.projects) for b in entry.bullets
    )
    assert "Kubernetes" not in all_text


def test_requirement_is_covered_from_a_profile_skill_without_a_model_judgement():
    resume = _generate(_payload(requirements=[]))

    coverage = {item.requirement: item.status for item in resume.requirements}
    assert coverage["Python"] == "covered"
    assert coverage["Kubernetes"] == "missing"


def test_coverage_cites_the_tech_fact_that_names_the_concept_not_the_entry_title():
    resume = _generate(_payload(requirements=[]))
    python = next(item for item in resume.requirements if item.requirement == "Python")

    assert python.facts == ["P1.tech"]
    assert "Python" in python.quote
    assert "Apex Ledger" not in python.quote


def test_reactjs_job_requirement_matches_the_profile_react_skill():
    profile = _profile()
    profile.work[0].tech = ["Python", "React"]
    resume = _generate(_payload(), profile=profile)
    coverage = {item.requirement: item.status for item in resume.requirements}

    assert coverage["ReactJS"] == "covered"


def test_coverage_derives_apis_and_design_patterns_from_skills():
    profile = _profile()
    profile.skills.append(
        Skill(
            name="Idempotency",
            canonical_name="idempotency",
            category=SkillCategory.concept,
            proficiency=Proficiency.strong,
        )
    )
    profile.projects[0].outcomes = "Implemented idempotency"
    parsed = ParsedJD(
        title="Backend engineer",
        must_have_skills=["APIs", "software design patterns"],
        nice_to_have_skills=[],
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    coverage = {item.requirement: item.status for item in resume.requirements}

    assert coverage["APIs"] == "covered"
    assert coverage["software design patterns"] == "covered"
    items = {item.requirement: item for item in resume.requirements}
    assert items["APIs"].facts == ["P1.tech"]
    assert "FastAPI" in items["APIs"].quote
    assert items["software design patterns"].facts == ["P1.outcomes.s1"]
    assert "idempotency" in items["software design patterns"].quote.lower()


def test_data_structures_stays_missing_even_with_a_caching_fact():
    profile = _profile()
    profile.projects[0].outcomes = "Implemented caching, circuit breaker and idempotency"
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["data structures"], nice_to_have_skills=[]
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    coverage = {item.requirement: item.status for item in resume.requirements}

    assert coverage["data structures"] == "missing"
    assert "data structures" in resume.missing_requirements


def test_data_structures_is_covered_when_a_fact_names_one():
    profile = _profile()
    profile.projects[0].outcomes = "Used a priority queue and a binary tree"
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["data structures"], nice_to_have_skills=[]
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    item = next(i for i in resume.requirements if i.requirement == "data structures")

    assert item.status == "covered"
    assert item.facts == ["P1.outcomes.s1"]


def test_data_structures_is_not_covered_by_a_bare_plural_verb():
    profile = _profile()
    profile.projects[0].outcomes = "Queues high-volume events through Apache Kafka"
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["data structures"], nice_to_have_skills=[]
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    coverage = {item.requirement: item.status for item in resume.requirements}

    assert coverage["data structures"] == "missing"
    assert "data structures" in resume.missing_requirements


def test_data_structures_is_covered_by_a_plural_noun_with_a_premodifier():
    profile = _profile()
    profile.projects[0].outcomes = "Processed items through message queues and multiple stacks"
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["data structures"], nice_to_have_skills=[]
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    item = next(i for i in resume.requirements if i.requirement == "data structures")

    assert item.status == "covered"
    assert item.facts == ["P1.outcomes.s1"]


def test_data_structures_is_not_covered_by_a_full_stack_phrase():
    profile = _profile()
    profile.work[0].role = "Full Stack Developer"
    profile.projects[0].outcomes = "Built a full stack application"
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["data structures"], nice_to_have_skills=[]
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    coverage = {item.requirement: item.status for item in resume.requirements}

    assert coverage["data structures"] == "missing"
    assert "data structures" in resume.missing_requirements


def test_data_structures_is_covered_by_an_algorithm_phrase():
    profile = _profile()
    profile.projects[0].outcomes = "Applied dynamic programming and binary search for indexing"
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["algorithms"], nice_to_have_skills=[]
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    item = next(i for i in resume.requirements if i.requirement == "algorithms")

    assert item.status == "covered"
    assert item.facts == ["P1.outcomes.s1"]


def test_system_design_patterns_is_not_satisfied_by_a_database_alone():
    profile = _profile()
    profile.skills = [
        Skill(
            name="PostgreSQL",
            canonical_name="postgresql",
            category=SkillCategory.database,
            proficiency=Proficiency.strong,
        )
    ]
    profile.projects[0].tech = ["PostgreSQL"]
    profile.work[0].tech = ["PostgreSQL"]
    parsed = ParsedJD(
        title="Backend engineer",
        must_have_skills=["system design patterns"],
        nice_to_have_skills=[],
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    coverage = {item.requirement: item.status for item in resume.requirements}

    assert coverage["system design patterns"] == "missing"


def test_familiar_cloud_skills_give_partial_cloud_coverage():
    profile = _profile()
    profile.skills.append(
        Skill(
            name="AWS",
            canonical_name="aws",
            category=SkillCategory.cloud,
            proficiency=Proficiency.familiar,
        )
    )
    parsed = ParsedJD(
        title="Backend engineer", must_have_skills=["cloud infrastructure"], nice_to_have_skills=[]
    )

    resume = _generate(_payload(requirements=[]), profile=profile, parsed_jd=parsed)
    coverage = {item.requirement: item.status for item in resume.requirements}

    assert coverage["cloud infrastructure"] == "partial"


# --------------------------------------------------------------------------- #
# Refusal + warnings
# --------------------------------------------------------------------------- #


def test_refuses_when_an_internship_has_no_description():
    profile = _profile()
    profile.work = []
    profile.internships = [
        Experience(role="Intern", company="GameGenesis", duration_months=3, tech=["Python"])
    ]

    resume = _generate(_payload(), profile=profile)

    assert resume.refused is True
    assert resume.refusal_reason
    assert "Profile page" in resume.refusal_reason
    assert "GameGenesis" in resume.refusal_reason
    assert resume.profile_gaps
    assert resume.summary == []


def test_refuses_and_lists_all_gaps_when_the_profile_is_thin():
    profile = _profile()
    profile.projects = []
    profile.links = Links()
    profile.work = [Experience(role="Data engineer", company="Acme", tech=["Python"])]

    gaps = profile_gaps(profile)

    assert any("no description lines" in gap for gap in gaps)
    assert "no projects" in gaps
    assert any("no links" in gap for gap in gaps)


def test_refusal_never_calls_the_llm():
    class _ExplodingClient:
        tokens_used = 0

        def call_json(self, **kwargs):
            raise AssertionError("the LLM must not be called for a thin profile")

    profile = _profile()
    profile.projects = []
    profile.links = Links()

    resume = generate_tailored_resume(
        _ExplodingClient(), profile=profile, job=_job(), parsed_jd=_parsed_jd()
    )

    assert resume.refused is True


def test_thin_internship_emits_a_warning_and_is_not_padded():
    profile = _profile()
    profile.internships = [
        Experience(
            role="Intern",
            company="GameGenesis",
            duration_months=3,
            tech=["Python"],
            description=["Builds a job pipeline"],
        )
    ]
    internship_bullet = {"text": "Build a job pipeline in Python.", "facts": ["I1.s1"]}
    work_bullet = {"text": "Build a job pipeline in Python.", "facts": ["W1.s1"]}

    resume = _generate(
        _payload(experience=[{"bullets": [internship_bullet]}, {"bullets": [work_bullet]}]),
        profile=profile,
    )

    assert len(resume.experience[0].bullets) == 1
    assert any("internship section thin" in warning for warning in resume.warnings)


# --------------------------------------------------------------------------- #
# Lexical grounding, sentence facts, repair pass (PR C1d)
# --------------------------------------------------------------------------- #


def test_prose_fields_are_split_into_sentence_facts():
    profile = _profile()
    profile.work[0].description = ["First sentence here.", "Second sentence here."]

    facts = build_facts(profile)

    assert facts["W1.s1"].text == "First sentence here."
    assert facts["W1.s2"].text == "Second sentence here."
    assert facts["W1.s1"].kind == "prose"
    assert "W1.description" not in facts


def test_bullet_with_content_words_absent_from_the_cited_sentence_is_dropped():
    profile = _profile()
    profile.work[0].description = [
        (
            "Sole engineer at an early-stage startup, owning the core backend, CRM and "
            "production systems, and building them from scratch with FastAPI and PostgreSQL."
        )
    ]
    bullet = {
        "text": (
            "Designed and built backend APIs with FastAPI and PostgreSQL, "
            "enabling real-time data access."
        ),
        "facts": ["W1.s1"],
    }

    resume = _generate(_payload(experience=[{"bullets": [bullet]}]), profile=profile)

    assert resume.experience[0].bullets == []
    assert any("content words not in cited facts" in line.reason for line in resume.dropped)


def test_a_stemmed_strength_word_is_rejected_without_a_licensing_fact():
    bullet = {"text": "Delivering robust APIs.", "facts": ["W1.s1"]}

    resume = _generate(_payload(experience=[{"bullets": [bullet]}]))

    assert resume.experience[0].bullets == []
    assert any("delivering" in line.reason for line in resume.dropped)


def test_repair_call_fills_a_project_the_first_draft_left_empty():
    project_bullet = {
        "text": "Shipped a live job pipeline.",
        "facts": ["P1.outcomes.s1", "P1.summary.s1"],
    }
    first = _payload(projects=[{"bullets": []}])
    second = {"experience": [], "projects": [{"bullets": [project_bullet]}]}
    calls = {"n": 0}

    def handler(kwargs):
        calls["n"] += 1
        return json.dumps(first if calls["n"] == 1 else second)

    resume = generate_tailored_resume(
        LLMClient(client=FakeClient(handler)),
        profile=_profile(),
        job=_job(),
        parsed_jd=_parsed_jd(),
    )

    assert calls["n"] == 2
    assert [b.text for b in resume.projects[0].bullets] == [project_bullet["text"]]


def test_repair_call_fills_a_thin_internship_section():
    profile = _profile()
    profile.internships = [
        Experience(
            role="Intern",
            company="GameGenesis",
            duration_months=3,
            tech=["Python"],
            description=[
                "Built the core API in Python.",
                "Shipped the reporting dashboard.",
                "Fixed the caching layer.",
            ],
        )
    ]
    first = _payload(experience=[{"bullets": []}, {"bullets": []}])
    second = {
        "experience": [
            {
                "bullets": [
                    {"text": "Built the core API in Python.", "facts": ["I1.s1"]},
                    {"text": "Shipped the reporting dashboard.", "facts": ["I1.s2"]},
                    {"text": "Fixed the caching layer.", "facts": ["I1.s3"]},
                ]
            },
            {"bullets": []},
        ],
        "projects": [],
    }
    calls = {"n": 0}

    def handler(kwargs):
        calls["n"] += 1
        return json.dumps(first if calls["n"] == 1 else second)

    resume = generate_tailored_resume(
        LLMClient(client=FakeClient(handler)),
        profile=profile,
        job=_job(),
        parsed_jd=_parsed_jd(),
    )

    assert calls["n"] == 2
    assert len(resume.experience[0].bullets) == 3
    assert not any("internship section thin" in warning for warning in resume.warnings)


def test_a_familiar_skill_evidenced_by_a_tech_list_is_promoted_to_main():
    profile = _profile()
    profile.projects[0].tech = ["Python", "FastAPI", "Flask"]
    profile.skills.append(
        Skill(
            name="Flask",
            canonical_name="flask",
            category=SkillCategory.framework,
            proficiency=Proficiency.familiar,
        )
    )

    skills, familiar = build_skills(profile, _parsed_jd(), get_ontology())

    assert "Flask" in skills
    assert "Flask" not in familiar


def test_project_tech_is_cleaned_and_becomes_an_evidence_linked_skill():
    profile = _profile()
    profile.projects[0].tech = ["Python", "and Vercel."]

    resume = _generate(_payload(), profile=profile)

    assert resume.projects[0].tech == ["Python", "Vercel"]
    assert "Vercel" in resume.additional_tools
    assert "Vercel" not in resume.skills


def test_clean_tech_items_strips_leading_and_trailing_period_and_dedupes():
    assert clean_tech_items(["and Vercel.", "  and Render. ", "Vercel", "", "  "]) == [
        "Vercel",
        "Render",
    ]


def test_summary_opens_with_a_code_built_sentence():
    resume = _generate(_payload())

    first = resume.summary[0]
    assert first.text.startswith("Mid backend developer with 30 months of experience in ")
    assert first.text.endswith(".")
    assert first.facts == []


def test_code_summary_uses_internship_months_and_top_skills():
    profile = _profile()
    profile.internships = [
        Experience(
            role="Intern",
            company="GameGenesis",
            duration_months=3,
            tech=["Python"],
            description=["Builds a job pipeline"],
        )
    ]
    profile.experience_level = "fresher"
    profile.target_roles = ["Backend"]

    resume = _generate(_payload(), profile=profile)

    first = resume.summary[0].text
    assert first.startswith("Fresher backend developer with 3 months of internship experience in ")
    assert "Python" in first


def test_a_project_only_summary_sentence_is_not_used():
    project_only = {
        "text": "Shipped a live job pipeline.",
        "facts": ["P1.outcomes.s1", "P1.summary.s1"],
    }

    resume = _generate(_payload(summary=[project_only]))

    assert project_only["text"] not in [line.text for line in resume.summary]


def test_adjacent_technology_coverage_is_downgraded_to_partial():
    profile = _profile()
    profile.projects[0].summary = "Builds a job pipeline with JWT authentication"
    parsed = ParsedJD(
        title="Backend engineer",
        must_have_skills=["security awareness"],
        nice_to_have_skills=[],
        confidence=0.9,
    )
    requirement = {
        "requirement": "security awareness",
        "status": "covered",
        "facts": ["P1.summary.s1"],
        "quote": "JWT authentication",
    }

    resume = _generate(
        _payload(requirements=[requirement]),
        profile=profile,
        parsed_jd=parsed,
    )

    item = next(i for i in resume.requirements if i.requirement == "security awareness")
    assert item.status == "partial"
    assert item.facts == ["P1.summary.s1"]


# --------------------------------------------------------------------------- #
# Structural fields + ATS lint
# --------------------------------------------------------------------------- #


def test_dates_and_employers_are_preserved_from_the_profile():
    resume = _generate(_payload())

    entry = resume.experience[0]
    assert entry.role == "Data engineer"
    assert entry.company == "Acme"
    assert entry.duration_months == 30
    assert resume.projects[0].title == "Atlas Ops"
    assert resume.projects[0].tech == ["Python", "FastAPI"]
    assert resume.education[0]["institution"] == "University of X"
    assert resume.certifications == ["AWS Certified"]


def test_ats_lint_passes_a_clean_layout_and_flags_a_table():
    resume = _generate(_payload())
    clean = ats_lint(resume_blocks(resume))
    assert clean.passed is True
    assert clean.reasons == []

    with_table = ats_lint([ResumeBlock(kind="table", text="Skills"), *resume_blocks(resume)])
    assert with_table.passed is False
    assert any("table" in reason for reason in with_table.reasons)


def test_ats_lint_flags_hidden_text_and_missing_heading():
    blocks = [
        ResumeBlock(kind="paragraph", text="hi\u200b"),
        ResumeBlock(kind="heading", text="X"),
    ]
    report = ats_lint(blocks)

    assert report.passed is False
    assert any("hidden" in reason for reason in report.reasons)
    assert any("missing standard heading" in reason for reason in report.reasons)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_strip_proficiency_removes_only_a_trailing_tag():
    assert strip_proficiency("Python (strong)") == "Python"
    assert strip_proficiency("Docker (working)") == "Docker"
    assert strip_proficiency("Rust (familiar)") == "Rust"
    assert strip_proficiency("Git/GitHub") == "Git/GitHub"


def test_tokenize_splits_on_punctuation_and_singularizes():
    assert _tokenize("Git/GitHub APIs") == ["git", "github", "api"]
    assert _tokenize("dependencies") == ["dependency"]


def test_build_facts_excludes_skills_and_numbers_entries():
    facts = build_facts(_profile())

    assert facts["P1.summary.s1"].text == "Builds a job pipeline"
    assert set(facts) == {
        "P1.title",
        "P1.summary.s1",
        "P1.outcomes.s1",
        "P1.tech",
        "W1.role",
        "W1.company",
        "W1.s1",
        "W1.tech",
        "ED1.line",
        "CERT",
        "LINK",
    }


def test_render_profile_facts_marks_skills_as_context_only():
    rendered = render_profile_facts(_profile())

    assert "[W1.s1] Builds a job pipeline" in rendered
    assert "context only" in rendered
    assert "never cite as evidence" in rendered


def test_build_skills_orders_jd_matches_first():
    skills, familiar = build_skills(_profile(), _parsed_jd(), get_ontology())

    assert skills[0] == "Python"
    assert "FastAPI" in skills
    assert "Rust" in familiar


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_profile_and_job_text_never_reach_braintrust():
    from test_observability import install_sdk

    from atlas.observability import reset_observability

    profile_marker = "zzprofilemarker9f3a"
    jd_marker = "zzjdmarker9f3a"
    profile = _profile()
    profile.skills.append(
        Skill(name=profile_marker, canonical_name=profile_marker, category=SkillCategory.other)
    )
    job = _job()
    job.description_raw = f"{job.description_raw} {jd_marker}"
    client = _client(_payload())

    sdk = install_sdk()
    try:
        generate_tailored_resume(client, profile=profile, job=job, parsed_jd=_parsed_jd())
    finally:
        reset_observability()

    assert sdk.spans, "expected at least one Braintrust span"
    blob = "".join(span.sent() for span in sdk.spans).lower()
    assert profile_marker not in blob
    assert jd_marker not in blob


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #


def test_generation_under_caps_records_tokens_to_the_daily_budget(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)

    class _CountingClient:
        def __init__(self, inner: LLMClient, per_call: int) -> None:
            self._inner = inner
            self.per_call = per_call
            self.tokens_used = 0

        def call_json(self, **kwargs):
            self.tokens_used += self.per_call
            return self._inner.call_json(**kwargs)

    client = _CountingClient(_client(_payload()), per_call=100)
    before = tokens_used_today(conn)

    generate_tailored_resume_under_caps(
        conn, client, profile=_profile(), job=_job(), parsed_jd=_parsed_jd()
    )

    assert tokens_used_today(conn) == before + 100


def test_generation_under_caps_enforces_the_per_run_call_cap(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)
    settings = get_settings().model_copy(deep=True)
    settings.run.max_llm_calls_per_run = 0

    with pytest.raises(LLMCallBudgetExceeded):
        generate_tailored_resume_under_caps(
            conn,
            _client(_payload()),
            profile=_profile(),
            job=_job(),
            parsed_jd=_parsed_jd(),
            settings=settings,
        )


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _conn_with_job(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _created = upsert_job(conn, run_id, _job())
    return conn, job_id


def test_save_tailored_resume_increments_version_per_job(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)

    first = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")
    second = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")

    assert first != second
    assert get_tailored_resume(conn, first)["version"] == 1
    assert get_tailored_resume(conn, second)["version"] == 2
    assert latest_tailored_resume(conn, job_id)["id"] == second
    assert get_tailored_resume(conn, first)["approved_at"] is None


def test_approve_sets_approved_at_and_marks_the_version_downloadable(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)
    resume_id = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")

    assert approved_tailored_resume(conn, job_id) is None
    approve_tailored_resume(conn, resume_id)

    row = get_tailored_resume(conn, resume_id)
    assert row["approved_at"] is not None
    assert approved_tailored_resume(conn, job_id)["id"] == resume_id


def test_approve_is_idempotent_and_keeps_the_first_timestamp(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)
    resume_id = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")

    approve_tailored_resume(conn, resume_id)
    stamp = get_tailored_resume(conn, resume_id)["approved_at"]
    approve_tailored_resume(conn, resume_id)

    assert get_tailored_resume(conn, resume_id)["approved_at"] == stamp


def test_approve_unknown_resume_raises_key_error(tmp_path):
    conn, _job_id = _conn_with_job(tmp_path)

    with pytest.raises(KeyError):
        approve_tailored_resume(conn, 9999)


def test_latest_tailored_resume_is_none_without_a_build(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)

    assert latest_tailored_resume(conn, job_id) is None


def test_add_llm_tokens_accumulates(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)

    add_llm_tokens(conn, 40)
    add_llm_tokens(conn, 2)

    assert tokens_used_today(conn) == 42


# --------------------------------------------------------------------------- #
# C1e: empty sections get a verbatim fallback + warning
# --------------------------------------------------------------------------- #


def _two_empty_projects() -> list[Project]:
    return [
        Project(title="Alpha", tech=["Python"], summary="Builds Alpha", outcomes="live"),
        Project(title="Beta", tech=["Python"], summary="Builds Beta", outcomes="live"),
    ]


def test_empty_project_gets_a_verbatim_fallback_and_a_warning():
    profile = _profile()
    profile.projects = _two_empty_projects()[:1]

    resume = _generate(_payload(projects=[{"bullets": []}]), profile=profile)

    assert [b.text for b in resume.projects[0].bullets] == ["Builds Alpha"]
    assert resume.projects[0].bullets[0].source == PROFILE_VERBATIM
    assert any("profile-verbatim" in w for w in resume.warnings)
    assert any("Alpha" in w for w in resume.warnings)


def test_each_empty_project_gets_its_own_repair_call():
    profile = _profile()
    profile.projects = _two_empty_projects()
    draft = _payload(projects=[{"bullets": []}, {"bullets": []}])
    seen: list[str] = []

    def handler(kwargs):
        user = kwargs["messages"][-1]["content"]
        if "Repair pass (one project)" in user:
            if "Project 1 (" in user:
                seen.append("Alpha")
                return json.dumps(
                    {
                        "projects": [
                            {
                                "bullets": [
                                    {
                                        "text": "Build a live Alpha.",
                                        "facts": ["P1.summary.s1", "P1.outcomes.s1"],
                                    }
                                ]
                            }
                        ]
                    }
                )
            seen.append("Beta")
            return json.dumps(
                {
                    "projects": [
                        {
                            "bullets": [
                                {
                                    "text": "Build a live Beta.",
                                    "facts": ["P2.summary.s1", "P2.outcomes.s1"],
                                }
                            ]
                        }
                    ]
                }
            )
        return json.dumps(draft)

    resume = generate_tailored_resume(
        LLMClient(client=FakeClient(handler)),
        profile=profile,
        job=_job(),
        parsed_jd=_parsed_jd(),
    )

    assert seen == ["Alpha", "Beta"]
    assert [b.text for b in resume.projects[0].bullets] == ["Build a live Alpha."]
    assert [b.text for b in resume.projects[1].bullets] == ["Build a live Beta."]


def test_resume_call_budget_is_capped_then_falls_back_to_verbatim():
    profile = _profile()
    profile.projects = _two_empty_projects()
    draft = _payload(projects=[{"bullets": []}, {"bullets": []}])
    calls = {"n": 0}

    def handler(kwargs):
        calls["n"] += 1
        return json.dumps(draft)

    resume = generate_tailored_resume(
        LLMClient(client=FakeClient(handler)),
        profile=profile,
        job=_job(),
        parsed_jd=_parsed_jd(),
    )

    assert calls["n"] == MAX_RESUME_CALLS
    assert [b.text for b in resume.projects[0].bullets] == ["Builds Alpha"]
    assert [b.text for b in resume.projects[1].bullets] == ["Builds Beta"]
    assert sum("profile-verbatim" in w for w in resume.warnings) == 2


def test_thin_internship_is_backfilled_verbatim_and_warns():
    profile = _profile()
    profile.work = []
    profile.internships = [
        Experience(
            role="Intern",
            company="Globex",
            duration_months=6,
            tech=["Python"],
            description=[
                "Builds a job pipeline for the team.",
                "Writes MySQL queries for reports.",
                "Reviews pull requests weekly.",
            ],
        )
    ]
    internship_bullet = {"text": "Build a job pipeline in Python.", "facts": ["I1.s1"]}
    draft = _payload(
        summary=[{"text": "Builds a job pipeline.", "facts": ["I1.s1"]}],
        experience=[{"bullets": [internship_bullet]}],
        projects=[{"bullets": [VALID_PROJECT_BULLET]}],
    )
    calls = {"n": 0}

    def handler(kwargs):
        calls["n"] += 1
        return json.dumps(draft)

    resume = generate_tailored_resume(
        LLMClient(client=FakeClient(handler)),
        profile=profile,
        job=_job(),
        parsed_jd=_parsed_jd(),
    )

    assert calls["n"] == MAX_RESUME_CALLS
    texts = [b.text for b in resume.experience[0].bullets]
    assert "Writes MySQL queries for reports." in texts
    assert resume.experience[0].bullets[-1].source == PROFILE_VERBATIM
    assert any("profile-verbatim" in w for w in resume.warnings)


def test_resume_calls_use_temperature_zero():
    temps: list[float] = []

    def handler(kwargs):
        temps.append(kwargs["temperature"])
        return json.dumps(_payload())

    client = LLMClient(client=FakeClient(handler), temperature=0.9)
    generate_tailored_resume(client, profile=_profile(), job=_job(), parsed_jd=_parsed_jd())

    assert temps
    assert all(t == 0.0 for t in temps)


# --------------------------------------------------------------------------- #
# C1e: skills cap, alias dedupe, hosting exclusion, ordering
# --------------------------------------------------------------------------- #


def test_skills_are_capped_and_aliases_are_deduped():
    profile = _profile()
    aliases = [
        "JWT",
        "JWT Auth",
        "PostgreSQL",
        "PostgreSQL (Neon)",
        "Neon",
        "GitHub Actions",
        "CI/CD (GitHub Actions)",
        "Git/GitHub",
    ]
    profile.projects[0].tech = ["Python", *aliases, *[f"Tool{i}" for i in range(1, 12)]]

    resume = _generate(_payload(), profile=profile)

    assert len(resume.skills) == MAX_MAIN_SKILLS
    assert "JWT" in resume.skills
    assert "JWT Auth" not in resume.skills
    assert resume.skills.count("PostgreSQL") == 1
    assert "Neon" not in resume.skills
    assert "GitHub Actions" in resume.skills
    assert "Additional tools" not in resume.skills
    assert resume.additional_tools


def test_hosting_tools_are_excluded_unless_the_jd_names_them():
    profile = _profile()
    profile.projects[0].tech = ["Python", "Render", "Vercel", "Braintrust"]

    resume = _generate(_payload(), profile=profile)
    assert "Render" in resume.additional_tools
    assert "Vercel" in resume.additional_tools
    assert "Render" not in resume.skills
    assert "Vercel" not in resume.skills

    named = _generate(
        _payload(),
        profile=profile,
        parsed_jd=ParsedJD(
            title="Backend engineer",
            must_have_skills=["Python", "Vercel"],
            nice_to_have_skills=[],
            confidence=0.9,
        ),
    )
    assert "Vercel" in named.skills


def test_main_skills_order_by_cited_facts_after_jd_matches():
    profile = _profile()
    profile.projects[0].tech = ["Python", "FastAPI", "Docker"]
    profile.work[0].tech = ["Python", "Docker"]

    main, _familiar, _additional = classify_skills(profile, _parsed_jd(), get_ontology())

    assert main[0] == "Python"
    assert main.index("Docker") < main.index("FastAPI")


# --------------------------------------------------------------------------- #
# C1e: summary overlap rule
# --------------------------------------------------------------------------- #


def test_second_summary_line_is_omitted_when_it_duplicates_a_bullet():
    duplicate = {"text": "Build a job pipeline in Python.", "facts": ["W1.s1"]}

    resume = _generate(_payload(summary=[duplicate]))

    assert [line.text for line in resume.summary] == [resume.summary[0].text]
    assert len(resume.summary) == 1


def test_distinct_second_summary_line_is_kept():
    profile = _profile()
    profile.work[0].description = ["Builds a job pipeline.", "Writes MySQL queries."]
    line = {"text": "Writes MySQL queries.", "facts": ["W1.s2"]}

    resume = _generate(_payload(summary=[line]), profile=profile)

    assert line["text"] in [item.text for item in resume.summary]


# --------------------------------------------------------------------------- #
# C1e: dates rendered at output only
# --------------------------------------------------------------------------- #


def test_format_dates_renders_proper_case_and_leaves_unknown_text():
    assert format_dates("2026-03-23 - 2026-06-30") == "March 23, 2026 - June 30, 2026"
    assert format_dates("Jun 2024 - Aug 2024") == "June 2024 - August 2024"
    assert format_dates("2026-03") == "March 2026"
    assert format_dates("2020 - Present") == "2020 - Present"
    assert format_dates("tbd") == "tbd"
    assert format_dates(None) == ""
    assert format_dates("") == ""


def test_experience_dates_render_formatted_but_the_fact_stays_verbatim():
    profile = _profile()
    profile.work[0].dates = "2026-03-23 - 2026-06-30"

    resume = _generate(_payload(), profile=profile)

    assert resume.experience[0].dates == "March 23, 2026 - June 30, 2026"
    assert build_facts(profile)["W1.dates"].text == "2026-03-23 - 2026-06-30"


# --------------------------------------------------------------------------- #
# C1e: cache returns the same output with zero calls
# --------------------------------------------------------------------------- #


def test_cached_generation_returns_the_same_resume_with_zero_calls(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)
    fake = FakeClient(lambda kwargs: json.dumps(_payload()))
    client = LLMClient(client=fake)

    first = generate_tailored_resume_cached(
        conn,
        client,
        profile=_profile(),
        job=_job(),
        job_id=job_id,
        parsed_jd=_parsed_jd(),
    )
    assert first.cached is False
    assert first.calls > 0
    transport_calls = len(fake.chat.completions.calls)
    assert transport_calls == first.calls

    second = generate_tailored_resume_cached(
        conn,
        client,
        profile=_profile(),
        job=_job(),
        job_id=job_id,
        parsed_jd=_parsed_jd(),
    )

    assert second.cached is True
    assert second.tokens == 0
    assert second.calls == 0
    assert len(fake.chat.completions.calls) == transport_calls
    assert first.resume.model_dump() == second.resume.model_dump()
