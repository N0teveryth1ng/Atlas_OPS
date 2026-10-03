from atlas.config import Settings
from atlas.query_planner import MAX_QUERIES, plan_queries
from atlas.schemas import CandidateProfile, Proficiency, Skill


def _profile(*, roles=("Backend Developer",), skills=("Python", "Django")):
    return CandidateProfile(
        target_roles=list(roles),
        skills=[
            Skill(name=name, canonical_name=name.lower(), proficiency=Proficiency.working)
            for name in skills
        ],
    )


def test_plan_queries_includes_level_terms_and_role():
    queries = plan_queries(_profile(), Settings())
    lowered = [q.lower() for q in queries]
    assert "backend developer" in lowered
    assert any(q.startswith("junior ") for q in lowered)
    assert any(q.startswith("fresher ") for q in lowered)
    assert any(q.startswith("graduate ") for q in lowered)
    assert any(q.startswith("entry level ") for q in lowered)


def test_plan_queries_includes_skill_variants():
    queries = plan_queries(_profile(), Settings())
    assert any("python" in q.lower() for q in queries)


def test_plan_queries_dedupes_and_caps():
    queries = plan_queries(_profile(roles=("Dev",) * 1), Settings())
    assert len(queries) == len(set(queries))
    assert len(queries) <= MAX_QUERIES


def test_plan_queries_falls_back_to_config_roles():
    settings = Settings()
    settings.targets.target_roles = ["Data Analyst"]
    queries = plan_queries(CandidateProfile(), settings)
    assert any("data analyst" in q.lower() for q in queries)


def test_plan_queries_uses_default_role_when_empty():
    queries = plan_queries(CandidateProfile(), Settings())
    assert any("software engineer" in q.lower() for q in queries)
