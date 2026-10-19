"""What each endpoint does with good input: create, list, get, and update tickets."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func, select

from app.routers.tickets import find_customer, get_or_create_customer
from app.models import Customer, Message, MessageSender, Ticket

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def add_ticket(session_factory, phone="+16145550123", **fields):
    """Save a ticket straight to the test database, for tests that need a set created_at."""
    with session_factory() as db:
        customer = get_or_create_customer(db, "Jane Doe", phone)
        ticket = Ticket(customer=customer, subject="Order late", **fields)
        db.add(ticket)
        db.commit()
        return ticket.id


def count_rows(session_factory, model):
    with session_factory() as db:
        return db.scalar(select(func.count()).select_from(model))


def parse_utc(text):
    """Read a datetime from the API, and check that it's in UTC."""
    value = datetime.fromisoformat(text)
    assert value.utcoffset() == timedelta(0), text
    return value


def ids(response):
    return [ticket["id"] for ticket in response.json()]


def test_root_says_the_api_is_running(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Support API is running"}


# Create and get


def test_create_returns_201_with_the_fields_the_server_fills_in(client, ticket_data):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.post("/tickets", json=ticket_data)

    assert response.status_code == 201
    ticket = response.json()
    assert isinstance(ticket["id"], int)
    assert ticket["customer"] == {
        "id": ticket["customer"]["id"],
        "name": "Jane Doe",
        "phone": "+16145550123",
    }
    assert ticket["agent_id"] is None
    assert ticket["subject"] == "Order late"
    assert ticket["status"] == "open"
    assert ticket["priority"] == "medium"
    assert parse_utc(ticket["created_at"]) >= before
    assert ticket["closed_at"] is None


def test_each_new_ticket_gets_its_own_id(make_ticket):
    assert make_ticket()["id"] != make_ticket()["id"]


def test_the_message_becomes_the_first_in_the_conversation(client, make_ticket):
    ticket = make_ticket(message="Where is my order?")
    response = client.get(f"/tickets/{ticket['id']}/messages")

    assert response.status_code == 200
    [message] = response.json()
    assert message["sender"] == "customer"
    assert message["body"] == "Where is my order?"
    assert message["created_at"] == ticket["created_at"]


def test_a_returning_customer_is_found_by_phone_and_keeps_their_first_name(
    make_ticket, session_factory
):
    first = make_ticket(customer_name="Jane Doe", phone="+16145550123")
    second = make_ticket(customer_name="Jane D.", phone="+16145550123")

    assert second["customer"] == first["customer"]
    assert second["customer"]["name"] == "Jane Doe"
    assert count_rows(session_factory, Customer) == 1


def test_a_new_phone_number_is_a_new_customer(make_ticket, session_factory):
    first = make_ticket(customer_name="Jane Doe", phone="+16145550123")
    second = make_ticket(customer_name="Jane Doe", phone="+16145550124")

    assert first["customer"]["id"] != second["customer"]["id"]
    assert count_rows(session_factory, Customer) == 2


def test_a_customer_added_by_another_request_mid_lookup_is_reused(session_factory, monkeypatch):
    # Another request adds the customer after this one's lookup missed. The insert hits the
    # unique constraint on phone, and the request must use the existing customer, not fail.
    with session_factory() as db:
        db.add(Customer(name="Jane Doe", phone="+16145550123"))
        db.commit()

    lookups = []

    def find_customer_that_misses_once(db, phone):
        lookups.append(phone)
        # The first lookup ran just before the other request saved the customer.
        return None if len(lookups) == 1 else find_customer(db, phone)

    monkeypatch.setattr("app.routers.tickets.find_customer", find_customer_that_misses_once)

    with session_factory() as db:
        customer = get_or_create_customer(db, "Jane D.", "+16145550123")
        assert customer.name == "Jane Doe"
        # The failed insert didn't spoil the transaction: the rest of the request still works.
        db.add(Ticket(customer=customer, subject="Order late"))
        db.commit()

    assert len(lookups) == 2
    assert count_rows(session_factory, Customer) == 1
    assert count_rows(session_factory, Ticket) == 1


def test_get_returns_the_saved_ticket(client, make_ticket):
    ticket = make_ticket(customer_name="Ravi Kumar")
    response = client.get(f"/tickets/{ticket['id']}")
    assert response.status_code == 200
    assert response.json() == ticket


def test_get_a_missing_ticket_returns_404(client):
    response = client.get("/tickets/1")
    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}


# Messages


def test_messages_come_oldest_first(client, session_factory):
    ticket_id = add_ticket(session_factory, created_at=NOW)
    with session_factory() as db:
        # Saved out of order, to show the order comes from created_at.
        for hours, sender, body in [
            (2, MessageSender.customer, "Thanks, that fixed it!"),
            (0, MessageSender.customer, "Where is my order?"),
            (1, MessageSender.agent, "It ships today."),
        ]:
            created_at = NOW + timedelta(hours=hours)
            db.add(Message(ticket_id=ticket_id, sender=sender, body=body, created_at=created_at))
        db.commit()

    messages = client.get(f"/tickets/{ticket_id}/messages").json()
    assert [(m["sender"], m["body"]) for m in messages] == [
        ("customer", "Where is my order?"),
        ("agent", "It ships today."),
        ("customer", "Thanks, that fixed it!"),
    ]


def test_messages_of_a_missing_ticket_returns_404(client):
    response = client.get("/tickets/1/messages")
    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}


# List


def test_list_is_empty_when_there_are_no_tickets(client):
    response = client.get("/tickets")
    assert response.status_code == 200
    assert response.json() == []


def test_list_returns_newest_first(client, session_factory):
    middle = add_ticket(session_factory, created_at=NOW - timedelta(days=1))
    newest = add_ticket(session_factory, created_at=NOW)
    oldest = add_ticket(session_factory, created_at=NOW - timedelta(days=2))
    assert ids(client.get("/tickets")) == [newest, middle, oldest]


def test_list_breaks_ties_with_the_newest_id_first(client, session_factory):
    first, second, third = (add_ticket(session_factory, created_at=NOW) for _ in range(3))
    assert ids(client.get("/tickets")) == [third, second, first]


def test_list_filters_by_status_and_priority(client, make_ticket):
    open_low = make_ticket()["id"]
    closed_urgent = make_ticket()["id"]
    open_urgent = make_ticket()["id"]
    client.patch(f"/tickets/{open_low}", json={"priority": "low"})
    client.patch(f"/tickets/{closed_urgent}", json={"status": "closed", "priority": "urgent"})
    client.patch(f"/tickets/{open_urgent}", json={"priority": "urgent"})

    assert set(ids(client.get("/tickets?status=open"))) == {open_low, open_urgent}
    assert ids(client.get("/tickets?status=closed")) == [closed_urgent]
    assert ids(client.get("/tickets?status=pending")) == []
    assert set(ids(client.get("/tickets?priority=urgent"))) == {closed_urgent, open_urgent}
    assert ids(client.get("/tickets?status=open&priority=urgent")) == [open_urgent]


def test_list_pages_through_tickets_with_skip_and_limit(client, session_factory):
    # Each ticket is an hour older than the one before, so this is the order the list returns.
    newest_first = [
        add_ticket(session_factory, created_at=NOW - timedelta(hours=hours)) for hours in range(5)
    ]

    assert ids(client.get("/tickets?limit=2")) == newest_first[0:2]
    assert ids(client.get("/tickets?skip=2&limit=2")) == newest_first[2:4]
    assert ids(client.get("/tickets?skip=4&limit=2")) == newest_first[4:5]
    assert ids(client.get("/tickets?skip=5&limit=2")) == []


def test_list_returns_20_tickets_by_default(client, session_factory):
    for _ in range(25):
        add_ticket(session_factory)
    assert len(client.get("/tickets").json()) == 20


def test_list_loads_customers_in_one_query_not_one_per_ticket(client, engine, session_factory):
    for number in range(20):
        add_ticket(session_factory, phone=f"+161455501{number:02d}")

    statements = []

    def record(connection, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get("/tickets")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len({ticket["customer"]["phone"] for ticket in response.json()}) == 20
    # One query finds the logged-in agent. Then one gets the page of tickets, and one gets all
    # their customers. Loading each customer on its own would take 21 instead of 2.
    assert len(statements) == 3
    assert "FROM agents" in statements[0]


# Update


def test_update_changes_only_the_priority(client, make_ticket):
    ticket = make_ticket()
    response = client.patch(f"/tickets/{ticket['id']}", json={"priority": "high"})

    assert response.status_code == 200
    assert response.json() == {**ticket, "priority": "high"}


def test_update_changes_status_and_priority_together(client, make_ticket):
    ticket = make_ticket()
    response = client.patch(
        f"/tickets/{ticket['id']}", json={"status": "pending", "priority": "urgent"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["priority"] == "urgent"


def test_update_is_saved(client, make_ticket):
    ticket = make_ticket()
    client.patch(f"/tickets/{ticket['id']}", json={"status": "pending"})
    assert client.get(f"/tickets/{ticket['id']}").json()["status"] == "pending"


def test_a_null_field_in_an_update_is_left_alone(client, make_ticket):
    ticket = make_ticket()
    response = client.patch(f"/tickets/{ticket['id']}", json={"status": None, "priority": "low"})
    assert response.json()["status"] == "open"
    assert response.json()["priority"] == "low"


def test_closing_a_ticket_records_when_it_closed(client, make_ticket):
    ticket = make_ticket()
    before = datetime.now(timezone.utc)
    response = client.patch(f"/tickets/{ticket['id']}", json={"status": "closed"})
    after = datetime.now(timezone.utc)

    assert response.json()["status"] == "closed"
    assert before <= parse_utc(response.json()["closed_at"]) <= after


def test_closing_an_already_closed_ticket_keeps_the_first_close_time(client, make_ticket):
    ticket = make_ticket()
    first = client.patch(f"/tickets/{ticket['id']}", json={"status": "closed"}).json()
    second = client.patch(f"/tickets/{ticket['id']}", json={"status": "closed"}).json()
    assert second["closed_at"] == first["closed_at"]


def test_changing_the_priority_of_a_closed_ticket_keeps_its_close_time(client, make_ticket):
    ticket = make_ticket()
    closed = client.patch(f"/tickets/{ticket['id']}", json={"status": "closed"}).json()
    updated = client.patch(f"/tickets/{ticket['id']}", json={"priority": "low"}).json()
    assert updated["status"] == "closed"
    assert updated["closed_at"] == closed["closed_at"]


def test_reopening_a_ticket_clears_its_close_time(client, make_ticket):
    for new_status in ["open", "pending"]:
        ticket = make_ticket()
        client.patch(f"/tickets/{ticket['id']}", json={"status": "closed"})
        response = client.patch(f"/tickets/{ticket['id']}", json={"status": new_status})
        assert response.json()["status"] == new_status
        assert response.json()["closed_at"] is None


def test_update_a_missing_ticket_returns_404(client):
    response = client.patch("/tickets/1", json={"status": "closed"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}
