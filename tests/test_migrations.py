"""Migrations build the schema the models describe, and every one of them can be undone."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.config import PROJECT_ROOT
from app.database import Base

# Migrations that tests run up to, by revision id.
CREATE_TICKETS = "086e8c9716c3"
ADD_CUSTOMERS = "5a8da3b2d2f4"
INDEX_TICKETS = "a8dd85a3d297"
ADD_AGENT_PASSWORDS = "89711c13eabd"


@pytest.fixture
def scripts():
    return ScriptDirectory.from_config(Config(PROJECT_ROOT / "alembic.ini"))


def test_migrations_form_a_single_line(scripts):
    # Two heads means two migrations claim the same parent, usually from two branches.
    # Alembic then can't tell which comes first, and `upgrade head` refuses to run.
    assert len(scripts.get_heads()) == 1


def test_upgrade_builds_the_schema_the_models_describe(empty_database, migrate):
    migrate(empty_database, command.upgrade, "head")
    with empty_database.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    # Anything here means a model changed without a migration to match.
    assert differences == []


def test_downgrade_removes_every_table_and_type(empty_database, migrate):
    migrate(empty_database, command.upgrade, "head")
    migrate(empty_database, command.downgrade, "base")

    database = inspect(empty_database)
    assert database.get_table_names() == ["alembic_version"]
    assert database.get_enums() == []


def test_each_migration_can_be_undone_and_redone(empty_database, migrate, scripts):
    # Climb one migration at a time, stepping back down and up again at each one.
    oldest_first = reversed(list(scripts.walk_revisions()))
    for revision in [script.revision for script in oldest_first]:
        migrate(empty_database, command.upgrade, revision)
        migrate(empty_database, command.downgrade, "-1")
        migrate(empty_database, command.upgrade, revision)


def rows(engine, sql):
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql))]


def test_adding_customers_moves_existing_tickets_into_customers_and_messages(
    empty_database, migrate
):
    migrate(empty_database, command.upgrade, CREATE_TICKETS)
    with empty_database.begin() as connection:
        # Tickets in the old shape. Jane wrote twice, with a different name the second time.
        connection.execute(
            text(
                """
                INSERT INTO tickets
                    (customer_name, phone, subject, message, status, priority, created_at)
                VALUES
                    ('Jane D.', '+16145550123', 'Refund', 'Any news?', 'open', 'high',
                     '2026-10-05 09:00+00'),
                    ('Jane Doe', '+16145550123', 'Order late', 'Where is it?', 'closed', 'low',
                     '2026-10-01 09:00+00'),
                    ('Ravi Kumar', '+447911123456', 'Login', 'I can''t log in.', 'pending',
                     'medium', '2026-10-03 09:00+00')
                """
            )
        )

    migrate(empty_database, command.upgrade, ADD_CUSTOMERS)

    # One customer per phone number, named as on their earliest ticket.
    assert rows(empty_database, "SELECT name, phone FROM customers ORDER BY created_at") == [
        ("Jane Doe", "+16145550123"),
        ("Ravi Kumar", "+447911123456"),
    ]
    tickets = """
        SELECT tickets.subject, customers.phone, tickets.status::text
        FROM tickets JOIN customers ON customers.id = tickets.customer_id
        ORDER BY tickets.id
    """
    assert rows(empty_database, tickets) == [
        ("Refund", "+16145550123", "open"),
        ("Order late", "+16145550123", "closed"),
        ("Login", "+447911123456", "pending"),
    ]
    messages = "SELECT ticket_id, sender::text, body FROM messages ORDER BY ticket_id"
    assert rows(empty_database, messages) == [
        (1, "customer", "Any news?"),
        (2, "customer", "Where is it?"),
        (3, "customer", "I can't log in."),
    ]


def test_removing_customers_puts_them_and_the_first_message_back_on_tickets(
    empty_database, migrate
):
    migrate(empty_database, command.upgrade, ADD_CUSTOMERS)
    with empty_database.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO customers (name, phone) VALUES ('Jane Doe', '+16145550123');
                INSERT INTO tickets (customer_id, subject, status, priority)
                    VALUES (1, 'Order late', 'open', 'medium'), (1, 'No messages', 'open', 'low');
                INSERT INTO messages (ticket_id, sender, body, created_at) VALUES
                    (1, 'agent', 'It ships today.', '2026-10-01 10:00+00'),
                    (1, 'customer', 'Where is my order?', '2026-10-01 09:00+00');
                """
            )
        )

    migrate(empty_database, command.downgrade, CREATE_TICKETS)

    old_shape = "SELECT customer_name, phone, subject, message FROM tickets ORDER BY id"
    assert rows(empty_database, old_shape) == [
        ("Jane Doe", "+16145550123", "Order late", "Where is my order?"),
        ("Jane Doe", "+16145550123", "No messages", ""),
    ]


def test_agents_already_in_the_table_get_no_password_and_stay_active(empty_database, migrate):
    migrate(empty_database, command.upgrade, INDEX_TICKETS)
    with empty_database.begin() as connection:
        connection.execute(
            text("INSERT INTO agents (name, email) VALUES ('Sam Lee', 'sam.lee@example.com')")
        )

    migrate(empty_database, command.upgrade, ADD_AGENT_PASSWORDS)

    # No one can log in as an existing agent until a password is set for them.
    assert rows(empty_database, "SELECT email, password_hash, is_active FROM agents") == [
        ("sam.lee@example.com", None, True)
    ]
