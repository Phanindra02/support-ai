"""The seed script makes realistic tickets: spread over 12 weeks, mostly closed when older."""

import re
from datetime import datetime, timedelta, timezone
from statistics import median

import pytest
from sqlalchemy import create_engine, func, select

from app.models import Ticket, TicketPriority, TicketStatus
from app.schemas import E164_PATTERN
from scripts.seed import WEEKS, add_fake_tickets, main, make_tickets

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def tickets():
    # A fixed seed and `now` make these checks give the same result every run.
    return make_tickets(1000, seed=1, now=NOW)


def hours_to_close(ticket):
    return (ticket.closed_at - ticket.created_at).total_seconds() / 3600


def test_makes_the_requested_number_of_tickets(tickets):
    assert len(tickets) == 1000


def test_phone_numbers_are_e164(tickets):
    assert all(re.fullmatch(E164_PATTERN, ticket.phone) for ticket in tickets)


def test_created_times_spread_over_the_last_12_weeks(tickets):
    created = [ticket.created_at for ticket in tickets]
    assert all(NOW - timedelta(weeks=WEEKS) <= moment <= NOW for moment in created)
    assert min(created) < NOW - timedelta(weeks=WEEKS - 1)
    assert max(created) > NOW - timedelta(weeks=1)


def test_only_closed_tickets_have_a_close_time_and_it_is_in_the_past(tickets):
    for ticket in tickets:
        if ticket.status == TicketStatus.closed:
            assert ticket.created_at < ticket.closed_at <= NOW
        else:
            assert ticket.closed_at is None


def test_most_older_tickets_are_closed_and_most_new_ones_are_not(tickets):
    def closed_share(group):
        return sum(ticket.status == TicketStatus.closed for ticket in group) / len(group)

    older = [t for t in tickets if t.created_at < NOW - timedelta(weeks=3)]
    last_day = [t for t in tickets if t.created_at > NOW - timedelta(days=1)]
    assert closed_share(older) > 0.9
    assert closed_share(last_day) < 0.5


def test_urgent_tickets_close_faster_than_low_ones(tickets):
    def median_hours(priority):
        return median(
            hours_to_close(t)
            for t in tickets
            if t.priority == priority and t.status == TicketStatus.closed
        )

    assert median_hours(TicketPriority.urgent) < median_hours(TicketPriority.high)
    assert median_hours(TicketPriority.high) < median_hours(TicketPriority.medium)
    assert median_hours(TicketPriority.medium) < median_hours(TicketPriority.low)


def test_every_status_and_priority_shows_up(tickets):
    assert {ticket.status for ticket in tickets} == set(TicketStatus)
    assert {ticket.priority for ticket in tickets} == set(TicketPriority)


def test_the_same_seed_makes_the_same_tickets():
    def summary(ticket):
        return (ticket.customer_name, ticket.phone, ticket.subject, ticket.created_at)

    first = make_tickets(20, seed=7, now=NOW)
    second = make_tickets(20, seed=7, now=NOW)
    assert [summary(t) for t in first] == [summary(t) for t in second]


def test_add_fake_tickets_saves_them_and_reset_clears_old_ones(session_factory):
    def count(db):
        return db.scalar(select(func.count()).select_from(Ticket))

    with session_factory() as db:
        add_fake_tickets(db, 30, seed=1)
        add_fake_tickets(db, 20, seed=2)
        assert count(db) == 50

        add_fake_tickets(db, 10, seed=3, reset=True)
        assert count(db) == 10


def test_seed_says_to_run_migrations_when_the_tables_are_missing(monkeypatch):
    monkeypatch.setattr("scripts.seed.engine", create_engine("sqlite://"))
    monkeypatch.setattr("sys.argv", ["seed"])
    with pytest.raises(SystemExit, match="alembic upgrade head"):
        main()
