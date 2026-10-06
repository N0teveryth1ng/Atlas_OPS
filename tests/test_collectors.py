from atlas.collectors import (
    AdzunaCollector,
    AshbyCollector,
    GreenhouseCollector,
    LeverCollector,
    RemoteOKCollector,
    RemotiveCollector,
    build_collectors,
)
from atlas.config import Settings


def test_adzuna_unavailable_without_credentials():
    collector = AdzunaCollector("", "", get_json=lambda url, params: {"results": []})
    assert collector.available is False
    assert collector.fetch("python") == []


def test_adzuna_maps_results():
    def fake(url, params):
        assert params["what"] == "python developer"
        return {
            "results": [
                {
                    "id": "42",
                    "title": "Python Developer",
                    "company": {"display_name": "Acme"},
                    "location": {"display_name": "Pune"},
                    "redirect_url": "http://adz/42",
                    "description": "<p>Build APIs</p>",
                    "created": "2024-01-01T00:00:00Z",
                }
            ]
        }

    jobs = AdzunaCollector("id", "key", get_json=fake).fetch("python developer")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "adzuna"
    assert job.title == "Python Developer"
    assert job.company == "Acme"
    assert job.location == "Pune"
    assert job.url == "http://adz/42"
    assert job.posted_at is not None


def test_greenhouse_filters_by_query_and_caches_board():
    calls = {"n": 0}

    def fake(url, params):
        calls["n"] += 1
        return {
            "jobs": [
                {
                    "id": 1,
                    "title": "Backend Engineer",
                    "absolute_url": "http://gh/1",
                    "location": {"name": "Remote"},
                    "content": "<p>desc</p>",
                    "updated_at": "2024-01-02T00:00:00Z",
                }
            ]
        }

    collector = GreenhouseCollector(["acme"], get_json=fake)
    assert len(collector.fetch("backend")) == 1
    assert collector.fetch("frontend") == []
    assert calls["n"] == 1


def test_lever_maps_postings():
    def fake(url, params):
        return [
            {
                "id": "x",
                "text": "Data Engineer",
                "hostedUrl": "http://lv/x",
                "categories": {"location": "Berlin"},
                "descriptionPlain": "desc",
                "createdAt": 1700000000000,
            }
        ]

    jobs = LeverCollector(["acme"], get_json=fake).fetch("")
    assert jobs[0].title == "Data Engineer"
    assert jobs[0].location == "Berlin"
    assert jobs[0].posted_at is not None


def test_ashby_maps_postings():
    def fake(url, params):
        return {
            "jobs": [
                {
                    "id": "a1",
                    "title": "Platform Engineer",
                    "jobUrl": "http://ash/a1",
                    "location": "Remote",
                    "descriptionPlain": "desc",
                    "publishedAt": "2024-02-02T00:00:00Z",
                }
            ]
        }

    jobs = AshbyCollector(["acme"], get_json=fake).fetch("")
    assert jobs[0].title == "Platform Engineer"
    assert jobs[0].url == "http://ash/a1"


def test_remotive_maps_search():
    def fake(url, params):
        assert params["search"] == "python"
        return {
            "jobs": [
                {
                    "id": 7,
                    "url": "http://rm/7",
                    "title": "Python Dev",
                    "company_name": "Acme",
                    "candidate_required_location": "Worldwide",
                    "description": "<p>d</p>",
                    "publication_date": "2024-03-01T00:00:00",
                }
            ]
        }

    jobs = RemotiveCollector(get_json=fake).fetch("python")
    assert jobs[0].remote_type.value == "remote"
    assert jobs[0].company == "Acme"


def test_remoteok_skips_legal_notice_and_filters():
    def fake(url, params):
        return [
            {"legal": "terms"},
            {
                "id": 1,
                "position": "Python Developer",
                "company": "Acme",
                "url": "http://rok/1",
                "description": "d",
                "date": "2024-04-01T00:00:00Z",
            },
        ]

    jobs = RemoteOKCollector(get_json=fake).fetch("python")
    assert len(jobs) == 1
    assert jobs[0].title == "Python Developer"


def test_safe_fetch_swallows_errors():
    def boom(url, params):
        raise RuntimeError("down")

    collector = RemoteOKCollector(get_json=boom)
    assert collector.safe_fetch("python") == []


def test_build_collectors_respects_toggles_and_companies():
    settings = Settings()
    settings.sources.remoteok = False
    settings.sources.remotive = False
    settings.sources.adzuna = False
    settings.sources.lever = False
    settings.sources.ashby = False
    collectors = build_collectors(
        settings, get_json=lambda url, params: {"jobs": []}, companies={"greenhouse": ["acme"]}
    )
    assert [c.name for c in collectors] == ["greenhouse"]


def test_build_collectors_skips_ats_without_companies():
    collectors = build_collectors(Settings(), get_json=lambda url, params: {}, companies={})
    names = {c.name for c in collectors}
    assert "greenhouse" not in names
    assert "lever" not in names
    assert "ashby" not in names


def test_himalayas_is_opt_in_and_needs_no_companies():
    """Off by default (its API terms require attribution), and needs no tokens."""
    off = build_collectors(Settings(), get_json=lambda url, params: {}, companies={})
    assert "himalayas" not in {c.name for c in off}

    settings = Settings()
    settings.sources.himalayas = True
    on = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "himalayas" in {c.name for c in on}


def test_arbeitnow_is_opt_in_and_needs_no_companies():
    off = build_collectors(Settings(), get_json=lambda url, params: {}, companies={})
    assert "arbeitnow" not in {c.name for c in off}

    settings = Settings()
    settings.sources.arbeitnow = True
    on = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "arbeitnow" in {c.name for c in on}


def test_jobicy_is_opt_in_and_needs_no_companies():
    off = build_collectors(Settings(), get_json=lambda url, params: {}, companies={})
    assert "jobicy" not in {c.name for c in off}

    settings = Settings()
    settings.sources.jobicy = True
    on = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "jobicy" in {c.name for c in on}


def test_weworkremotely_is_opt_in_and_needs_no_companies():
    off = build_collectors(Settings(), get_json=lambda url, params: {}, companies={})
    assert "weworkremotely" not in {c.name for c in off}

    settings = Settings()
    settings.sources.weworkremotely = True
    on = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "weworkremotely" in {c.name for c in on}


def test_smartrecruiters_is_opt_in_and_skipped_without_companies():
    settings = Settings()
    settings.sources.smartrecruiters = True
    without = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "smartrecruiters" not in {c.name for c in without}

    with_tokens = build_collectors(
        settings,
        get_json=lambda url, params: {},
        companies={"smartrecruiters": ["sportradar"]},
    )
    assert "smartrecruiters" in {c.name for c in with_tokens}


def test_workable_is_opt_in_and_skipped_without_companies():
    settings = Settings()
    settings.sources.workable = True
    without = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "workable" not in {c.name for c in without}

    with_tokens = build_collectors(
        settings,
        get_json=lambda url, params: {},
        companies={"workable": ["huckberry"]},
    )
    assert "workable" in {c.name for c in with_tokens}


def test_recruitee_is_opt_in_and_skipped_without_companies():
    settings = Settings()
    settings.sources.recruitee = True
    without = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "recruitee" not in {c.name for c in without}

    with_tokens = build_collectors(
        settings,
        get_json=lambda url, params: {},
        companies={"recruitee": ["sysmex"]},
    )
    assert "recruitee" in {c.name for c in with_tokens}


def test_build_collectors_passes_the_careers_sites_token_from_secrets():
    settings = Settings()
    settings.sources.recruitee = True
    settings.secrets.recruitee_careers_sites_token = "unit-token"
    collectors = build_collectors(settings, companies={"recruitee": ["sysmex"]})
    recruitee = next(c for c in collectors if c.name == "recruitee")
    assert recruitee.get_json.policy.headers == {"X-Careers-Sites-Token": "unit-token"}


def test_build_collectors_omits_the_token_header_when_the_secret_is_empty():
    settings = Settings()
    settings.sources.recruitee = True
    collectors = build_collectors(settings, companies={"recruitee": ["sysmex"]})
    recruitee = next(c for c in collectors if c.name == "recruitee")
    assert recruitee.get_json.policy.headers is None


def test_teamtailor_is_opt_in_and_skipped_without_companies():
    settings = Settings()
    settings.sources.teamtailor = True
    without = build_collectors(settings, get_json=lambda url, params: {}, companies={})
    assert "teamtailor" not in {c.name for c in without}

    with_tokens = build_collectors(
        settings,
        get_json=lambda url, params: {},
        companies={"teamtailor": ["varma"]},
    )
    assert "teamtailor" in {c.name for c in with_tokens}


def test_every_new_source_defaults_to_off():
    """None of the eight new sources may be live out of the box."""
    sources = Settings().sources
    for name in (
        "himalayas",
        "arbeitnow",
        "jobicy",
        "weworkremotely",
        "smartrecruiters",
        "workable",
        "recruitee",
        "teamtailor",
    ):
        assert getattr(sources, name) is False, f"{name} should be opt-in"
