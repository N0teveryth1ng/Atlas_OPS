import pytest

from atlas.config import get_settings
from atlas.digest import build_digest, render_html, render_text
from atlas.job_status import InvariantViolation, JobStatus
from atlas.pipeline import ProcessedJob
from atlas.schemas import (
    Evidence,
    EvidenceSource,
    FilterRejection,
    FilterResult,
    Job,
    Recommendation,
    Verdict,
    VerifierVerdict,
)
from atlas.skills import SkillMatch


def _job(i: int, title: str = "Junior Python Dev", company: str = "C") -> Job:
    return Job(
        source="t",
        url=f"http://x/{i}",
        dedupe_key=f"k{i}",
        title=title,
        company=company,
        location="Remote",
    )


def _result(i, rec, score=80.0, needs_review=False, status=JobStatus.ranked):
    verdict = Verdict(
        fit_score=score,
        seniority_fit=90,
        recommendation=rec,
        reasons_for=[
            Evidence(quote="JD asks for 0-2 years", source=EvidenceSource.jd),
            Evidence(quote="Python core stack", source=EvidenceSource.profile),
        ],
        reasons_against=[Evidence(quote="On-call rotation", source=EvidenceSource.jd)],
        seniority_assessment=Evidence(quote="Open to a fresher", source=EvidenceSource.jd),
    )
    return ProcessedJob(
        job_id=i,
        job=_job(i),
        filter_result=FilterResult(passed=True),
        skill_match=SkillMatch(
            must_have_coverage=0.8, nice_to_have_coverage=0.0, missing_must_haves=["Docker"]
        ),
        verdict=verdict,
        verifier=VerifierVerdict(),
        final_recommendation=rec,
        score=score,
        needs_review=needs_review,
        status=status,
    )


def test_build_digest_sections():
    results = [
        _result(1, Recommendation.strong_apply, 90),
        _result(2, Recommendation.apply, 70),
    ]
    digest = build_digest(results, get_settings())
    titles = [section.title for section in digest.sections]
    assert "Strong matches" in titles
    assert "Worth a look" in titles
    assert digest.summary.sent == 2
    assert digest.item_count == 2


def test_build_digest_raises_on_needs_review():
    results = [
        _result(3, Recommendation.maybe, 50, needs_review=True, status=JobStatus.needs_review)
    ]
    with pytest.raises(InvariantViolation):
        build_digest(results, get_settings())


def test_build_digest_summary_counts_rejections():
    rejected = ProcessedJob(
        job_id=9,
        job=_job(9),
        filter_result=FilterResult(
            passed=False,
            rejections=[FilterRejection(rule_id="senior_title", evidence="senior")],
        ),
        final_recommendation=Recommendation.skip,
    )
    digest = build_digest([_result(1, Recommendation.apply), rejected], get_settings())
    assert digest.summary.filtered_out == 1
    assert digest.summary.rejection_counts["senior_title"] == 1


def test_build_digest_skips_already_sent():
    digest = build_digest([_result(1, Recommendation.apply)], get_settings(), already_sent={1})
    assert digest.is_empty()


def test_render_contains_key_fields():
    digest = build_digest([_result(1, Recommendation.apply)], get_settings())
    text = render_text(digest)
    html = render_html(digest)
    assert "Junior Python Dev" in text
    assert "Docker" in text
    assert "Seniority:" in html
    assert "Risk:" in html


REMOTEOK_URL = "https://remoteok.com/remote-jobs/123"
HIMALAYAS_URL = "https://himalayas.app/jobs/python-dev"


def _merged_result(i: int = 1, urls: list[str] | None = None):
    """A result whose job carries the merged cross-source URLs of a deduped job.

    The canonical ``job.url`` stays on RemoteOK even though Himalayas also
    contributed - exactly the case where the required link-back used to vanish.
    """
    result = _result(i, Recommendation.apply)
    merged = urls if urls is not None else [REMOTEOK_URL, HIMALAYAS_URL]
    result.job = result.job.model_copy(update={"url": merged[0], "urls": list(merged)})
    return result


def test_credits_cover_every_contributing_source_in_order():
    digest = build_digest([_merged_result()], get_settings())
    item = digest.sections[0].items[0]
    assert [(c.name, c.url) for c in item.credits] == [
        ("remoteok", REMOTEOK_URL),
        ("himalayas", HIMALAYAS_URL),
    ]


def test_text_renderer_links_every_contributing_source():
    digest = build_digest([_merged_result()], get_settings())
    text = render_text(digest)
    # Existing lines intact: the canonical RemoteOK URL is still the first link.
    assert f"    {REMOTEOK_URL}" in text
    # Additive: Himalayas is credited with its own direct link even though the
    # canonical job.url belongs to RemoteOK.
    assert f"    via remoteok: {REMOTEOK_URL}" in text
    assert f"    via himalayas: {HIMALAYAS_URL}" in text


def test_html_renderer_links_every_contributing_source():
    digest = build_digest([_merged_result()], get_settings())
    html = render_html(digest)
    assert f"<a href='{REMOTEOK_URL}'" in html
    assert f"<a href='{HIMALAYAS_URL}'" in html
    assert ">remoteok</a>" in html
    assert ">himalayas</a>" in html


def test_unknown_host_urls_are_still_credited():
    unknown = "https://example.com/careers/9"
    digest = build_digest([_merged_result(urls=[unknown])], get_settings())
    item = digest.sections[0].items[0]
    assert [(c.name, c.url) for c in item.credits] == [("example.com", unknown)]
    assert f"    via example.com: {unknown}" in render_text(digest)
    assert f"<a href='{unknown}'" in render_html(digest)


def test_one_credit_per_source_keeps_the_first_url():
    second = "https://remoteok.com/remote-jobs/456"
    digest = build_digest(
        [_merged_result(urls=[REMOTEOK_URL, second, HIMALAYAS_URL])], get_settings()
    )
    item = digest.sections[0].items[0]
    assert [(c.name, c.url) for c in item.credits] == [
        ("remoteok", REMOTEOK_URL),
        ("himalayas", HIMALAYAS_URL),
    ]
    # No URL is ever lost: the uncredited duplicate is still on the item.
    assert second in item.urls


DANGEROUS_URL = "javascript:alert(document.cookie)"


def test_non_http_urls_never_become_credit_links():
    """Only absolute http(s) URLs may be turned into a clickable credit."""
    digest = build_digest(
        [_merged_result(urls=[DANGEROUS_URL, REMOTEOK_URL, HIMALAYAS_URL])], get_settings()
    )
    item = digest.sections[0].items[0]
    # Skipped before source lookup: no credit, no name, no href.
    assert [(c.name, c.url) for c in item.credits] == [
        ("remoteok", REMOTEOK_URL),
        ("himalayas", HIMALAYAS_URL),
    ]
    assert "href='javascript:" not in render_html(digest)
    # Kept as data - the URL is still on the item, it just never becomes a link.
    assert DANGEROUS_URL in item.urls
    assert f"    via himalayas: {HIMALAYAS_URL}" in render_text(digest)
    assert f"<a href='{HIMALAYAS_URL}'" in render_html(digest)


def test_title_link_is_only_made_for_http_urls():
    digest = build_digest([_merged_result(urls=[DANGEROUS_URL, REMOTEOK_URL])], get_settings())
    html = render_html(digest)
    assert "href='javascript:" not in html
    # The job still renders as a plain title rather than linking to the bad URL.
    assert "<strong>Junior Python Dev</strong>" in html
    assert f"<a href='{REMOTEOK_URL}'" in html


NEAR_MISS_URL = "https:job"


def test_scheme_without_a_host_is_not_a_link():
    """Right scheme, no host: ``https:job`` must not become an href either."""
    digest = build_digest(
        [_merged_result(urls=[NEAR_MISS_URL, REMOTEOK_URL, HIMALAYAS_URL])], get_settings()
    )
    item = digest.sections[0].items[0]
    assert [(c.name, c.url) for c in item.credits] == [
        ("remoteok", REMOTEOK_URL),
        ("himalayas", HIMALAYAS_URL),
    ]
    html = render_html(digest)
    assert "href='https:job'" not in html
    assert f"<a href='{REMOTEOK_URL}'" in html
    # Still kept as data on the item - dropped as a link, never as a URL.
    assert NEAR_MISS_URL in item.urls
