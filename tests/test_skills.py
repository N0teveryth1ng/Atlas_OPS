import pytest

from atlas.schemas import Proficiency, Skill
from atlas.skills import get_ontology, load_ontology, match_skills


def _profile(*names, proficiency=Proficiency.working):
    ontology = get_ontology()
    return [
        Skill(name=name, canonical_name=ontology.canonicalize(name), proficiency=proficiency)
        for name in names
    ]


def test_canonicalize_aliases():
    ontology = get_ontology()
    assert ontology.canonicalize("ReactJS") == "react"
    assert ontology.canonicalize("react.js") == "react"
    assert ontology.canonicalize("psql") == "postgresql"
    assert ontology.canonicalize("PostgreSQL") == "postgresql"
    assert ontology.canonicalize("K8s") == "kubernetes"
    assert ontology.canonicalize("CSharp") == "csharp"
    assert ontology.canonicalize("CPP") == "cpp"


def test_exact_and_alias_match_full_credit():
    match = match_skills(["ReactJS"], [], _profile("React", proficiency=Proficiency.strong))
    assert match.must_have_coverage == 1.0
    assert not match.missing_must_haves


def test_related_skill_partial_credit():
    match = match_skills(["Django"], [], _profile("Python"))
    assert 0 < match.must_have_coverage < 1
    assert not match.missing_must_haves


def test_unrelated_skill_missing():
    match = match_skills(["Java"], [], _profile("Python"))
    assert match.must_have_coverage == 0.0
    assert match.missing_must_haves == ["Java"]


def test_distinct_technologies_do_not_match():
    match = match_skills(["React Native"], [], _profile("React"))
    assert match.missing_must_haves == ["React Native"]


def test_proficiency_scales_credit():
    strong = match_skills(["Python"], [], _profile("Python", proficiency=Proficiency.strong))
    familiar = match_skills(["Python"], [], _profile("Python", proficiency=Proficiency.familiar))
    assert strong.must_have_coverage > familiar.must_have_coverage


def test_mixed_must_and_nice_coverage():
    match = match_skills(
        ["Python", "Java"], ["Docker"], _profile("Python", "Docker", proficiency=Proficiency.strong)
    )
    assert match.must_have_coverage == 0.5
    assert match.nice_to_have_coverage == 1.0
    assert match.missing_must_haves == ["Java"]


def test_ontology_alias_coverage_at_least_60():
    """Audit S1: the shipped ontology must expose a real alias set."""
    ontology = get_ontology()
    assert len(ontology.alias_to_canonical) - len(ontology.entries) >= 60


def test_spring_boot_alias_satisfies_spring():
    match = match_skills(["Spring"], [], _profile("Spring Boot"))
    assert match.must_have_coverage > 0
    assert not match.missing_must_haves


def test_dotnet_core_alias_satisfies_dotnet():
    match = match_skills([".NET"], [], _profile(".NET Core"))
    assert match.must_have_coverage > 0
    assert not match.missing_must_haves


@pytest.mark.parametrize(
    "jd,other",
    [
        ("Java", "JavaScript"),
        ("JavaScript", "Java"),
        ("C", "C#"),
        ("C#", "C"),
        ("React", "React Native"),
        ("React Native", "React"),
        ("Go", "GCP"),
    ],
)
def test_no_false_merge_between_distinct_skills(jd, other):
    """Audit S2: fuzzy fallback must not conflate distinct technologies."""
    match = match_skills([jd], [], _profile(other))
    assert match.missing_must_haves == [jd]


def test_load_ontology_from_custom_file(tmp_path):
    path = tmp_path / "skills.yaml"
    path.write_text(
        "skills:\n  rust:\n    display: Rust\n    aliases: [rs]\n    related: []\n",
        encoding="utf-8",
    )
    ontology = load_ontology(path)
    assert ontology.canonicalize("rs") == "rust"
    assert ontology.canonicalize("Rust") == "rust"
