import sys
import types

import pytest

from atlas.schemas import Proficiency, Skill
from atlas.skills import (
    OntologyEntry,
    SkillOntology,
    _cosine,
    _similarity,
    explain_skills,
    get_ontology,
    load_ontology,
    make_embedder,
    match_skills,
)


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


def test_ontology_entry_lookup():
    ontology = get_ontology()
    entry = ontology.entry("python")
    assert entry is not None and entry.canonical == "python"
    assert ontology.entry("definitely-not-a-skill") is None
    assert ontology.related("definitely-not-a-skill") == set()


def test_reverse_related_credit():
    ontology = SkillOntology()
    ontology.entries["a"] = OntologyEntry(canonical="a", related=["b"])
    ontology.alias_to_canonical["a"] = "a"
    profile = [Skill(name="a", canonical_name="a", proficiency=Proficiency.working)]
    match = match_skills(["b"], [], profile, ontology=ontology)
    assert match.must_have_coverage == 0.5


def test_cosine_handles_zero_norm():
    assert _cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)
    assert _cosine([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_similarity_uses_embedder_and_cache():
    calls = {"n": 0}

    def embedder(texts):
        calls["n"] += 1
        return [[1.0, 0.0] for _ in texts]

    cache: dict = {}
    first = _similarity("python", "python", embedder, cache)
    second = _similarity("python", "python", embedder, cache)
    assert first == second == pytest.approx(1.0)
    assert calls["n"] == 1


def test_make_embedder_wraps_sentence_transformer(monkeypatch):
    class _Vec:
        def tolist(self):
            return [[0.1, 0.2]]

    class _FakeST:
        def __init__(self, _name):
            pass

        def encode(self, _texts, normalize_embeddings=True):
            return _Vec()

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    embed = make_embedder()
    assert embed(["x"]) == [[0.1, 0.2]]


def test_load_ontology_from_custom_file(tmp_path):
    path = tmp_path / "skills.yaml"
    path.write_text(
        "skills:\n  rust:\n    display: Rust\n    aliases: [rs]\n    related: []\n",
        encoding="utf-8",
    )
    ontology = load_ontology(path)
    assert ontology.canonicalize("rs") == "rust"
    assert ontology.canonicalize("Rust") == "rust"


# --------------------------------------------------------------------------- #
# explain_skills: the per-skill credit breakdown
# --------------------------------------------------------------------------- #


def test_explain_skills_credits_match_the_coverage_figure():
    profile = _profile("Python", "Docker", proficiency=Proficiency.strong)
    must = ["Python", "Java"]
    match = match_skills(must, [], profile)
    details = explain_skills(must, profile)
    assert [d["skill"] for d in details] == must
    assert details[0]["matched"] is True
    assert details[1]["matched"] is False
    assert details[1]["credit"] == 0.0
    assert details[1]["why"] == "none"
    # The credits are the ones the coverage is built from.
    total = sum(float(d["credit"]) for d in details)
    assert total / len(must) == pytest.approx(match.must_have_coverage, abs=1e-4)


def test_explain_skills_names_the_proficiency_that_produced_the_credit():
    profile = _profile("Python", proficiency=Proficiency.familiar)
    details = explain_skills(["Python"], profile)
    assert details[0]["canonical"] == "python"
    assert "familiar" in details[0]["why"]


def test_explain_skills_marks_a_related_skill():
    details = explain_skills(["Django"], _profile("Python"))
    assert details[0]["matched"] is True
    assert details[0]["why"] == "related"
