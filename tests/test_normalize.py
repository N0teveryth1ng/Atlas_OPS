from atlas.normalize import (
    ATS_HOSTS,
    dedupe_jobs,
    description_signature,
    is_ats_url,
    make_dedupe_key,
    normalize_job,
    strip_boilerplate,
    strip_html,
)
from atlas.schemas import Job, RemoteType


def _job(**kwargs) -> Job:
    base = {
        "source": "test",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Pune",
        "url": "http://a",
    }
    base.update(kwargs)
    return Job(**base)


def test_strip_html_removes_tags_scripts_and_unescapes():
    raw = "<div>Hello&nbsp;<b>World</b><script>bad()</script></div>"
    assert strip_html(raw) == "Hello World"


def test_strip_boilerplate_cuts_at_eeo():
    text = "Great role. We are an equal opportunity employer and value diversity."
    assert strip_boilerplate(text) == "Great role."


def test_make_dedupe_key_is_stable_and_case_insensitive():
    a = make_dedupe_key("Acme", "Backend Engineer", "Pune")
    b = make_dedupe_key("  acme ", "backend   engineer", "PUNE")
    assert a == b


def test_normalize_job_sets_key_and_infers_remote():
    job = normalize_job(_job(description_raw="<p>Fully remote role</p>"))
    assert job.dedupe_key
    assert job.remote_type == RemoteType.remote
    assert job.description_raw == "Fully remote role"
    assert job.fetched_at is not None
    assert job.urls == ["http://a"]


def test_dedupe_merges_same_key_urls():
    first = normalize_job(_job(url="http://a"))
    second = normalize_job(_job(url="http://b"))
    merged = dedupe_jobs([first, second])
    assert len(merged) == 1
    assert merged[0].urls == ["http://a", "http://b"]


def test_dedupe_merges_cross_source_by_description_signature():
    desc = "Build services with Python. " * 20
    a = normalize_job(
        _job(
            source="remoteok",
            company="Acme",
            title="Backend Engineer",
            url="http://a",
            description_raw=desc,
        )
    )
    b = normalize_job(
        _job(
            source="remotive",
            company="Acme Inc",
            title="Software Engineer (Backend)",
            url="http://b",
            description_raw=desc,
        )
    )
    merged = dedupe_jobs([a, b])
    assert len(merged) == 1
    assert set(merged[0].urls) == {"http://a", "http://b"}


def test_description_signature_none_for_empty():
    assert description_signature("   ") is None


def test_is_ats_url_recognises_known_boards():
    assert is_ats_url("https://boards.greenhouse.io/acme/jobs/1")
    assert is_ats_url("https://jobs.lever.co/acme/abc-123")
    assert is_ats_url("https://jobs.ashbyhq.com/acme/abc")
    assert is_ats_url("https://apply.workable.com/acme/j/ABC/")
    assert is_ats_url("https://jobs.smartrecruiters.com/Acme/744")
    assert is_ats_url("https://acme.recruitee.com/api/offers")
    assert is_ats_url("https://acme.teamtailor.com/jobs/abc")


def test_is_ats_url_rejects_aggregators_and_junk():
    assert not is_ats_url("https://remoteok.com/jobs/1")
    assert not is_ats_url("https://weworkremotely.com/remote-jobs/acme")
    assert not is_ats_url("http://a")
    assert not is_ats_url("")
    assert not is_ats_url(None)


def test_dedupe_prefers_the_ats_url_as_canonical():
    """Same job on an aggregator and an ATS: show the first-party ATS page."""
    desc = "Own the platform end to end. " * 20
    aggregator = normalize_job(
        _job(
            source="weworkremotely",
            company="Acme",
            title="Backend Engineer",
            url="https://weworkremotely.com/remote-jobs/acme-backend",
            description_raw=desc,
        )
    )
    ats = normalize_job(
        _job(
            source="greenhouse",
            company="Acme",
            title="Backend Engineer",
            url="https://boards.greenhouse.io/acme/jobs/1",
            description_raw=desc,
        )
    )
    merged = dedupe_jobs([aggregator, ats])
    assert len(merged) == 1
    assert merged[0].url == "https://boards.greenhouse.io/acme/jobs/1"
    # Both URLs stay available.
    assert set(merged[0].urls) == {
        "https://weworkremotely.com/remote-jobs/acme-backend",
        "https://boards.greenhouse.io/acme/jobs/1",
    }


def test_dedupe_promotes_ats_regardless_of_arrival_order():
    desc = "Own the platform end to end. " * 20
    ats = normalize_job(
        _job(
            source="lever",
            company="Acme",
            title="Backend Engineer",
            url="https://jobs.lever.co/acme/abc-123",
            description_raw=desc,
        )
    )
    aggregator = normalize_job(
        _job(
            source="jobicy",
            company="Acme",
            title="Backend Engineer",
            url="https://jobicy.com/jobs/154612",
            description_raw=desc,
        )
    )
    merged = dedupe_jobs([ats, aggregator])
    assert len(merged) == 1
    # Already the canonical URL on arrival, so it must not be replaced.
    assert merged[0].url == "https://jobs.lever.co/acme/abc-123"


def test_dedupe_leaves_aggregator_only_urls_alone():
    desc = "Own the platform end to end. " * 20
    a = normalize_job(
        _job(
            source="remoteok",
            company="Acme",
            title="Backend Engineer",
            url="https://remoteok.com/jobs/1",
            description_raw=desc,
        )
    )
    b = normalize_job(
        _job(
            source="remotive",
            company="Acme",
            title="Backend Engineer",
            url="https://remotive.com/remote-jobs/1",
            description_raw=desc,
        )
    )
    merged = dedupe_jobs([a, b])
    assert len(merged) == 1
    # First arrival wins when neither side is an ATS.
    assert merged[0].url == "https://remoteok.com/jobs/1"


def test_ats_hosts_are_unique_and_lowercase():
    assert len(ATS_HOSTS) == len(set(ATS_HOSTS))
    assert all(host == host.lower() for host in ATS_HOSTS)
