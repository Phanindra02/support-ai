"""The cache measuring script times real misses and hits, and stops if they aren't."""

import pytest

from app.config import settings
from scripts.measure_cache import p95, time_stats


@pytest.fixture(autouse=True)
def few_requests(monkeypatch):
    monkeypatch.setattr("scripts.measure_cache.REQUESTS", 3)


def test_times_misses_and_then_hits(client, redis_client):
    # The test client is an httpx.Client, like the one the script uses.
    assert len(time_stats(client, redis_client, "MISS")) == 3
    assert len(time_stats(client, redis_client, "HIT")) == 3


def test_stops_if_a_request_is_not_the_kind_being_measured(client, redis_client):
    with pytest.raises(SystemExit, match="Expected a cache HIT, got MISS"):
        time_stats(client, redis_client, "HIT")


def test_p95_is_the_time_95_percent_of_requests_beat():
    assert p95([float(ms) for ms in range(1, 101)]) == pytest.approx(95.95)


def test_stops_with_advice_when_the_rate_limit_is_hit(client, redis_client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    with pytest.raises(SystemExit, match="RATE_LIMIT_PER_MINUTE=100000"):
        time_stats(client, redis_client, "MISS")
