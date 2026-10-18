"""GET /stats counts tickets, and caches the counts in Redis for 60 seconds."""

import logging
import time

from app.main import STATS_CACHE_SECONDS, STATS_KEY
from app.models import Customer, Ticket, TicketPriority, TicketStatus
from scripts.seed import main

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


# Clearing the cache when tickets change


def test_creating_a_ticket_clears_the_cached_stats(client, make_ticket):
    client.get("/stats")
    make_ticket()
    response = client.get("/stats")
    assert response.headers["X-Cache"] == "MISS"
    assert response.json()["by_status"]["open"] == 1


def test_updating_a_ticket_clears_the_cached_stats(client, make_ticket):
    ticket = make_ticket()
    client.get("/stats")
    client.patch(f"/tickets/{ticket['id']}", json={"status": "closed"})
    response = client.get("/stats")
    assert response.headers["X-Cache"] == "MISS"
    assert response.json()["by_status"] == {"open": 0, "pending": 0, "closed": 1}


def test_reading_tickets_keeps_the_cached_stats(client, make_ticket):
    ticket = make_ticket()
    client.get("/stats")
    client.get("/tickets")
    client.get(f"/tickets/{ticket['id']}")
    client.get(f"/tickets/{ticket['id']}/messages")
    assert client.get("/stats").headers["X-Cache"] == "HIT"


def test_a_failed_change_keeps_the_cached_stats(client, make_ticket):
    ticket = make_ticket()
    client.get("/stats")
    assert client.patch("/tickets/999", json={"status": "closed"}).status_code == 404
    assert client.patch(f"/tickets/{ticket['id']}", json={}).status_code == 422
    assert client.post("/tickets", json={}).status_code == 422
    assert client.get("/stats").headers["X-Cache"] == "HIT"


def test_tickets_can_still_change_when_redis_is_down(client, ticket_data, redis_is_down):
    response = client.post("/tickets", json=ticket_data)
    assert response.status_code == 201
    update = client.patch(f"/tickets/{response.json()['id']}", json={"status": "closed"})
    assert update.status_code == 200


def test_seeding_clears_the_cached_stats(engine, session_factory, redis_client, monkeypatch):
    redis_client.set(STATS_KEY, "old counts")
    monkeypatch.setattr("scripts.seed.engine", engine)
    monkeypatch.setattr("scripts.seed.SessionLocal", session_factory)
    monkeypatch.setattr("scripts.seed.redis_client", redis_client)
    monkeypatch.setattr("sys.argv", ["seed", "--count", "5"])

    main()

    assert redis_client.get(STATS_KEY) is None
