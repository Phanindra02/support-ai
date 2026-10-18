"""The seed script makes realistic data: spread over 12 weeks, mostly closed when older."""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

import pytest
from sqlalchemy import create_engine, func, select

from app.models import Agent, Customer, Message, MessageSender, Ticket, TicketPriority, TicketStatus
from app.schemas import E164_PATTERN
from scripts.seed import PHONE_COUNT, WEEKS, add_fake_tickets, main, make_tickets, phone_number

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def tickets():
    # A fixed seed and `now` make these checks give the same result every run.
    return make_tickets(1000, seed=1, now=NOW)


def hours_to_close(ticket):
    return (ticket.closed_at - ticket.created_at).total_seconds() / 3600


def count_rows(db, model):
    return db.scalar(select(func.count()).select_from(model))


# Tickets made in memory


def test_makes_the_requested_number_of_tickets_oldest_first(tickets):
    assert len(tickets) == 1000
    created = [ticket.created_at for ticket in tickets]
    assert created == sorted(created)


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


def test_conversations_follow_the_ticket(tickets):
    for ticket in tickets:
        first, *replies = ticket.messages
        # Every ticket starts with the customer's message, sent when the ticket opened.
        assert first.sender == MessageSender.customer
        assert first.created_at == ticket.created_at
        # Open tickets are still waiting for an agent. Pending and closed ones got a reply.
        if ticket.status == TicketStatus.open:
            assert replies == []
        else:
            assert replies[0].sender == MessageSender.agent
        times = [message.created_at for message in ticket.messages]
        assert times == sorted(times)
        assert times[-1] <= (ticket.closed_at or NOW)


def test_the_same_seed_makes_the_same_tickets():
    def summary(ticket):
        return (ticket.subject, ticket.priority, ticket.created_at, ticket.messages[0].body)

    first = make_tickets(20, seed=7, now=NOW)
    second = make_tickets(20, seed=7, now=NOW)
    assert [summary(t) for t in first] == [summary(t) for t in second]


def test_phone_numbers_are_e164_and_all_different():
    phones = [phone_number(n) for n in range(PHONE_COUNT)]
    assert all(re.fullmatch(E164_PATTERN, phone) for phone in phones)
    assert len(set(phones)) == PHONE_COUNT


# Saved to the database


def test_add_fake_tickets_saves_customers_agents_and_messages(session_factory):
    with session_factory() as db:
        add_fake_tickets(db, 300, seed=1, now=NOW)
        tickets = db.scalars(select(Ticket)).all()
        customers = db.scalars(select(Customer)).all()

        assert len(tickets) == 300
        assert count_rows(db, Agent) == 12
        assert count_rows(db, Message) > 300

        # Some customers come back with a new problem.
        tickets_per_customer = Counter(ticket.customer_id for ticket in tickets)
        assert len(customers) < 300
        assert max(tickets_per_customer.values()) > 1
        # Every saved customer opened a ticket, and joined when they opened their first.
        for customer in customers:
            assert customer.created_at == min(t.created_at for t in customer.tickets)

        # Only tickets someone has answered have an agent.
        for ticket in tickets:
            assert (ticket.agent_id is None) == (ticket.status == TicketStatus.open)


def test_tickets_are_saved_in_batches_without_mixing_up_customers(session_factory, monkeypatch):
    # Batches of 50 means 300 tickets take 6, so returning customers cross batches.
    monkeypatch.setattr("scripts.seed.BATCH_SIZE", 50)
    with session_factory() as db:
        add_fake_tickets(db, 300, seed=1, now=NOW)
        customers = db.scalars(select(Customer)).all()
        for customer in customers:
            assert customer.created_at == min(t.created_at for t in customer.tickets)
        assert count_rows(db, Ticket) == 300


def test_adding_more_keeps_the_agents_and_gives_new_customers_new_numbers(session_factory):
    with session_factory() as db:
        add_fake_tickets(db, 30, seed=1)
        add_fake_tickets(db, 20, seed=1)  # The same seed would pick the same numbers.
        assert count_rows(db, Ticket) == 50
        assert count_rows(db, Agent) == 12


def test_reset_clears_old_data(session_factory):
    with session_factory() as db:
        add_fake_tickets(db, 30, seed=1)
        add_fake_tickets(db, 10, seed=3, reset=True)
        assert count_rows(db, Ticket) == 10
        assert count_rows(db, Agent) == 12


def test_seed_says_to_run_migrations_when_the_tables_are_missing(monkeypatch):
    monkeypatch.setattr("scripts.seed.engine", create_engine("sqlite://"))
    monkeypatch.setattr("sys.argv", ["seed"])
    with pytest.raises(SystemExit, match="alembic upgrade head"):
        main()
