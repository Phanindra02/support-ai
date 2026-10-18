"""GET /stats counts tickets, and caches the counts in Redis for 60 seconds."""

import logging
import time

import pytest

from app.cache import get_redis, make_redis
from app.main import STATS_CACHE_SECONDS, STATS_KEY, app
from app.models import Customer, Ticket, TicketPriority, TicketStatus

ZERO_STATS = {
    "by_status": {"open": 0, "pending": 0, "closed": 0},
    "backlog_by_priority": {"low": 0, "medium": 0, "high": 0, "urgent": 0},
}


def add_tickets(session_factory, *status_and_priority):
    """Save tickets straight to the database, without going through the API."""
    with session_factory() as db:
        customer = Customer(name="Jane Doe", phone="+16145550123")
        for status, priority in status_and_priority:
            db.add(
                Ticket(customer=customer, subject="Order late", status=status, priority=priority)
            )
        db.commit()


def test_stats_with_no_tickets_are_all_zero(client):
    response = client.get("/stats")
    assert response.status_code == 200
    assert response.json() == ZERO_STATS


def test_stats_count_by_status_and_the_backlog_by_priority(client, session_factory):
    add_tickets(
        session_factory,
        (TicketStatus.open, TicketPriority.urgent),
        (TicketStatus.open, TicketPriority.urgent),
        (TicketStatus.pending, TicketPriority.low),
        (TicketStatus.closed, TicketPriority.urgent),
        (TicketStatus.closed, TicketPriority.high),
    )
    assert client.get("/stats").json() == {
        "by_status": {"open": 2, "pending": 1, "closed": 2},
        # Closed tickets aren't waiting on anyone, so they're not in the backlog.
        "backlog_by_priority": {"low": 1, "medium": 0, "high": 0, "urgent": 2},
    }


def test_the_first_request_counts_and_the_next_comes_from_the_cache(client):
    first = client.get("/stats")
    second = client.get("/stats")
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert second.json() == first.json()


def test_cached_stats_are_kept_for_60_seconds(client, redis_client):
    client.get("/stats")
    assert STATS_CACHE_SECONDS == 60
    assert 55 < redis_client.ttl(STATS_KEY) <= 60


def test_a_cache_hit_skips_the_database(client, session_factory):
    client.get("/stats")
    # Saved without the API, so nothing clears the cache.
    add_tickets(session_factory, (TicketStatus.open, TicketPriority.low))
    response = client.get("/stats")
    assert response.headers["X-Cache"] == "HIT"
    assert response.json() == ZERO_STATS


def test_when_the_cached_stats_expire_they_are_counted_again(client, session_factory, redis_client):
    client.get("/stats")
    add_tickets(session_factory, (TicketStatus.open, TicketPriority.low))
    redis_client.delete(STATS_KEY)  # What Redis does by itself after 60 seconds.

    response = client.get("/stats")
    assert response.headers["X-Cache"] == "MISS"
    assert response.json()["by_status"]["open"] == 1


@pytest.fixture
def redis_is_down(client):
    """Point the app at a Redis that isn't running. Nothing listens on port 1."""
    stopped = make_redis("redis://localhost:1/0")
    app.dependency_overrides[get_redis] = lambda: stopped
    yield
    stopped.close()


def test_stats_still_work_when_redis_is_down(client, session_factory, redis_is_down, caplog):
    add_tickets(session_factory, (TicketStatus.open, TicketPriority.low))
    with caplog.at_level(logging.WARNING, logger="app.cache"):
        start = time.perf_counter()
        response = client.get("/stats")
        seconds = time.perf_counter() - start

    assert response.status_code == 200
    # It gives up on Redis at once, instead of retrying for seconds.
    assert seconds < 0.5
    assert response.headers["X-Cache"] == "MISS"
    assert response.json()["by_status"]["open"] == 1
    assert "Redis failed" in caplog.text
