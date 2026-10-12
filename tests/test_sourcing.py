import time
from datetime import UTC, datetime, timedelta

from atlas.collectors.base import Collector
from atlas.config import Settings
from atlas.db import connect, init_db, start_run
from atlas.schemas import Job
from atlas.sourcing import collect, is_fresh

NOW = datetime(2024, 6, 1, tzinfo=UTC)


class FakeCollector(Collector):
    def __init__(self, name, jobs):
        super().__init__(get_json=lambda url, params: None)
        self.name = name
        self._jobs = jobs

    def fetch(self, query):
        return [job.model_copy(deep=True) for job in self._jobs]


def _job(**kwargs) -> Job:
    base = {
        "source": "fake",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Pune",
        "url": "http://a",
    }
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


def test_run_sourcing_defined_exactly_once():
    """Regression: a duplicated definition silently shadowed the first (audit T3)."""
    import ast
    import inspect

    from atlas import sourcing

    tree = ast.parse(inspect.getsource(sourcing))
    defs = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_sourcing"
    ]
    assert len(defs) == 1


class HangingCollector(Collector):
    query_based = True

    def __init__(self, name):
        super().__init__(get_json=lambda url, params: None)
        self.name = name

    def fetch(self, query):
        time.sleep(30)
        return []


class RaisingCollector(Collector):
    query_based = True

    def __init__(self, name):
        super().__init__(get_json=lambda url, params: None)
        self.name = name

    def fetch(self, query):
        raise RuntimeError("boom")


def test_source_timeout_is_recorded_and_the_run_continues():
    slow = HangingCollector("slow")
    good = FakeCollector("good", [_job(source="good")])

    started = time.monotonic()
    result = collect([slow, good], [""], settings=Settings(), now=NOW, source_timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 5, "a hung source must not hold up the run"
    assert "timed out" in result.source_errors["slow"]
    assert [item.source for item in result.source_yields] == ["slow", "good"]
    assert result.source_yields[1].fetched == 1
    assert len(result.jobs) == 1


def test_source_exception_is_recorded_and_the_run_continues():
    bad = RaisingCollector("bad")
    good = FakeCollector("good", [_job(source="good")])

    result = collect([bad, good], [""], settings=Settings(), now=NOW, source_timeout=5.0)

    assert result.source_errors["bad"] == "RuntimeError: boom"
    assert result.source_yields[0].fetched == 0
    assert result.source_yields[1].fetched == 1
    assert len(result.jobs) == 1


class SlowQueryCollector(Collector):
    query_based = True

    def __init__(self, name, delay, seen):
        super().__init__(get_json=lambda url, params: None)
        self.name = name
        self._delay = delay
        self._seen = seen

    def fetch(self, query):
        self._seen.append(query)
        time.sleep(self._delay)
        return []


def test_query_timeout_is_capped_by_the_remaining_run_budget():
    seen: list[str] = []
    slow = SlowQueryCollector("slow", 0.2, seen)

    result = collect(
        [slow],
        ["q1", "q2", "q3"],
        settings=Settings(),
        now=NOW,
        source_timeout=0.25,
        run_timeout=0.3,
    )

    assert "timed out" in result.source_errors["slow"]
    assert len(seen) < 3
    assert result.jobs == []


def test_whole_collection_timeout_skips_the_remaining_sources():
    a = HangingCollector("a")
    b = FakeCollector("b", [_job(source="b")])

    result = collect(
        [a, b], [""], settings=Settings(), now=NOW, source_timeout=5.0, run_timeout=0.15
    )

    assert "timed out" in result.source_errors["a"]
    assert result.source_errors["b"] == "collection run timed out"
    assert result.jobs == []
