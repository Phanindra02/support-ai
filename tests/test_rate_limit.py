"""Each IP address may make RATE_LIMIT_PER_MINUTE requests in any 60 seconds."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.rate_limit import seconds_until_allowed

LIMIT = 3
KEY = "ratelimit:203.0.113.7"
START = 1_000_000.0  # A made-up clock time, in seconds, so tests control the time.


def allowed(redis_client, at, key=KEY):
    return seconds_until_allowed(redis_client, key, LIMIT, START + at) == 0


# The algorithm, with a made-up clock


def test_requests_up_to_the_limit_are_allowed_and_the_next_is_not(redis_client):
    assert [allowed(redis_client, at=second) for second in range(LIMIT + 1)] == [
        True,
        True,
        True,
        False,
    ]


def test_a_rejected_request_waits_until_the_oldest_leaves_the_window(redis_client):
    for second in [0, 10, 20]:
        assert allowed(redis_client, at=second)
    # The request at 0 drops out of the window at 60, which is 30 seconds after 30.
    assert seconds_until_allowed(redis_client, KEY, LIMIT, START + 30) == pytest.approx(30)
    assert not allowed(redis_client, at=59.9)
    assert allowed(redis_client, at=60)


def test_the_window_slides_instead_of_resetting_each_minute(redis_client):
    # A full limit's worth at the end of one minute...
    for at in [57, 58, 59]:
        assert allowed(redis_client, at=at)
    # ...still counts at the start of the next. A counter that reset on the minute would
    # let these through, allowing twice the limit in a few seconds.
    assert not allowed(redis_client, at=61)
    assert not allowed(redis_client, at=116)
    assert allowed(redis_client, at=117)


def test_rejected_requests_do_not_use_up_the_limit(redis_client):
    for second in range(LIMIT):
        assert allowed(redis_client, at=second)
    for second in range(10, 20):
        assert not allowed(redis_client, at=second)
    # Once the first request leaves the window, there's room again, despite the 10 rejected.
    assert allowed(redis_client, at=60)


def test_each_ip_has_its_own_limit(redis_client):
    for second in range(LIMIT):
        assert allowed(redis_client, at=second)
    assert not allowed(redis_client, at=5)
    assert allowed(redis_client, at=5, key="ratelimit:198.51.100.4")


def test_the_log_of_a_quiet_ip_deletes_itself(redis_client):
    allowed(redis_client, at=0)
    assert 0 < redis_client.ttl(KEY) <= 60


# Through the API


@pytest.fixture
def low_limit(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", LIMIT)


def test_the_request_over_the_limit_gets_a_429_with_retry_after(client, low_limit):
    for _ in range(LIMIT):
        assert client.get("/tickets").status_code == 200

    response = client.get("/tickets")
    assert response.status_code == 429
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert response.json() == {"detail": f"Too many requests. Try again in {retry_after} seconds."}


def test_every_endpoint_counts_even_bad_requests(client, low_limit):
    assert client.get("/tickets/999").status_code == 404
    assert client.post("/tickets", json={}).status_code == 422
    assert client.get("/stats").status_code == 200
    assert client.get("/").status_code == 429


def test_another_ip_is_not_limited(client, low_limit):
    for _ in range(LIMIT + 1):
        client.get("/tickets")
    other_ip = TestClient(app, client=("198.51.100.4", 50000))
    assert other_ip.get("/tickets").status_code == 200


def test_the_docs_are_not_limited(client, low_limit):
    for _ in range(LIMIT + 1):
        client.get("/tickets")
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_the_docs_list_the_429_for_every_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        for method, operation in methods.items():
            assert "429" in operation["responses"], f"{method.upper()} {path}"


def test_requests_are_allowed_when_redis_is_down(client, low_limit, redis_is_down, caplog):
    with caplog.at_level(logging.WARNING, logger="app.rate_limit"):
        statuses = [client.get("/tickets").status_code for _ in range(LIMIT + 2)]
    assert statuses == [200] * (LIMIT + 2)
    assert "Skipped the rate limit" in caplog.text
