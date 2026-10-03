from datetime import datetime, timedelta, timezone

from atlas.collectors.base import Collector
from atlas.config import Settings
from atlas.db import connect, init_db, start_run
from atlas.schemas import Job
from atlas.sourcing import collect, is_fresh

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


class FakeCollector(Collector):
    def __init__(self, name, jobs):
        super().__init__(get_json=lambda url, params: None)
        self.name = name
        self._jobs = jobs

    def fetch(self, query):
        return [job.model_copy(deep=True) for job in self._jobs]


def _job(**kwargs) -> Job:
    base = dict(source="fake", title="Backend Engineer", company="Acme", location="Pune", url="http://a")
    base.update(kwargs)
    return Job(**base)


def test_is_fresh_boundaries():
    assert is_fresh(_job(), 14, NOW) is True
    old = _job(posted_at=NOW - timedelta(days=30))
    assert is_fresh(old, 14, NOW) is False


def test_collect_dedupes_filters_and_logs_yield():
    jobs = [
        _job(url="http://a", description_raw="<p>desc</p>"),
        _job(url="http://b", description_raw="<p>desc</p>"),
        _job(url="http://old", posted_at=NOW - timedelta(days=30)),
    ]
    source = FakeCollector("fake", jobs)
    conn = connect(":memory:")
    init_db(conn)
    run_id = start_run(conn, "test")

    result = collect([source], ["q1"], settings=Settings(), conn=conn, run_id=run_id, now=NOW)

    assert len(result.jobs) == 1
    assert result.dropped_stale == 1
    assert result.new_jobs == 1
    assert result.source_yields[0].fetched == 3
    assert result.source_yields[0].kept == 1
    assert result.query_yield["q1"] == 2
    conn.close()


def test_collect_merges_cross_source_duplicates():
    desc = "Long identical description. " * 20
    a = FakeCollector("src_a", [_job(source="src_a", url="http://a", description_raw=desc)])
    b = FakeCollector(
        "src_b",
        [_job(source="src_b", company="Other", title="SWE", url="http://b", description_raw=desc)],
    )
    result = collect([a, b], [""], settings=Settings(), now=NOW)
    assert len(result.jobs) == 1
    assert set(result.jobs[0].urls) == {"http://a", "http://b"}
