from datetime import datetime

from atlas.scheduler import next_run_delay, serve


def test_next_run_delay_before_hour():
    now = datetime(2026, 1, 1, 8, 0, 0)
    assert next_run_delay(now, 9) == 3600


def test_next_run_delay_after_hour_rolls_to_tomorrow():
    now = datetime(2026, 1, 1, 10, 0, 0)
    assert next_run_delay(now, 9) == 23 * 3600


def test_serve_runs_immediately_then_daily():
    runs: list[int] = []
    sleeps: list[float] = []
    clock = datetime(2026, 1, 1, 8, 0, 0)

    serve(
        lambda: runs.append(1),
        daily_hour=9,
        run_immediately=True,
        sleep=lambda seconds: sleeps.append(seconds),
        now=lambda: clock,
        max_iterations=1,
    )

    assert runs == [1, 1]
    assert sleeps == [3600.0]
