from atlas.normalize import (
    dedupe_jobs,
    description_signature,
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
