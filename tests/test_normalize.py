from atlas.normalize import (
    ATS_HOSTS,
    SOURCE_HOSTS,
    dedupe_jobs,
    description_signature,
    is_ats_url,
    make_dedupe_key,
    normalize_job,
    source_for_url,
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


def test_is_ats_url_only_matches_the_host_not_query_or_path():
    assert not is_ats_url("https://remoteok.com/jobs/1?next=boards.greenhouse.io")
    assert not is_ats_url("https://remoteok.com/jobs/1/boards.greenhouse.io/2")
    assert not is_ats_url("https://remoteok.com/jobs/1?ref=acme.recruitee.com")
    assert not is_ats_url("https://remoteok.com/jobs/acme.teamtailor.com/404")


def test_is_ats_url_still_recognises_every_known_host():
    assert is_ats_url("https://boards-api.greenhouse.io/acme/123")
    assert is_ats_url("https://sub.job-boards.greenhouse.io/acme/123")
    assert is_ats_url("https://hire.lever.co/acme/abc")
    assert is_ats_url("https://sub.jobs.ashbyhq.com/acme/abc")


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


def test_source_for_url_covers_every_direct_source():
    assert source_for_url("https://himalayas.app/jobs/python-dev") == "himalayas"
    assert source_for_url("https://jobicy.com/remote-jobs/1") == "jobicy"
    assert source_for_url("https://www.arbeitnow.com/jobs/1") == "arbeitnow"
    assert source_for_url("https://remoteok.com/remote-jobs/123") == "remoteok"
    assert source_for_url("https://www.remotive.com/remote-jobs/1") == "remotive"
    assert source_for_url("https://weworkremotely.com/remote-jobs/abc") == "weworkremotely"


def test_source_for_url_covers_every_ats_host():
    assert source_for_url("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert source_for_url("https://boards-api.greenhouse.io/acme/1") == "greenhouse"
    assert source_for_url("https://job-boards.greenhouse.io/acme/1") == "greenhouse"
    assert source_for_url("https://jobs.lever.co/acme/abc") == "lever"
    assert source_for_url("https://hire.lever.co/acme/abc") == "lever"
    assert source_for_url("https://jobs.ashbyhq.com/acme/abc") == "ashby"
    assert source_for_url("https://apply.workable.com/acme/j/ABC/") == "workable"
    assert source_for_url("https://jobs.smartrecruiters.com/Acme/744") == "smartrecruiters"
    assert source_for_url("https://sysmex.recruitee.com/jobs/1") == "recruitee"
    assert source_for_url("https://acme.teamtailor.com/jobs/abc") == "teamtailor"


def test_source_for_url_uses_the_same_dot_suffix_discipline_as_is_ats_url():
    assert source_for_url("https://sub.jobs.ashbyhq.com/acme/abc") == "ashby"
    assert source_for_url("https://sub.job-boards.greenhouse.io/acme/1") == "greenhouse"
    # A hostname that merely ends without a dot boundary is not a match.
    assert source_for_url("https://evilrecruitee.com/jobs/1") is None
    assert source_for_url("https://notremoteok.com/jobs/1") is None


def test_source_for_url_ignores_known_hosts_in_the_path_or_query():
    assert source_for_url("https://example.com/jobs/1?ref=boards.greenhouse.io") is None
    assert source_for_url("https://example.com/boards.greenhouse.io/1") is None
    assert source_for_url("https://example.com/jobs/1?next=acme.recruitee.com") is None


def test_source_for_url_unknown_and_empty_hosts_are_none():
    assert source_for_url("https://example.com/careers/1") is None
    assert source_for_url("http://a") is None
    assert source_for_url("") is None
    assert source_for_url(None) is None


def test_source_hosts_are_lowercase_and_reach_every_ats_host():
    assert all(host == host.lower() for host, _ in SOURCE_HOSTS)
    names = {host for host, _ in SOURCE_HOSTS}
    assert set(ATS_HOSTS) <= names
