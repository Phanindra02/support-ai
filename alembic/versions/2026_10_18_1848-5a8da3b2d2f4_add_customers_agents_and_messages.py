"""Add customers, agents, and messages

Moves each ticket's customer name and phone into a customers table, and its message into a
messages table, keeping the data that's already there.

Revision ID: 5a8da3b2d2f4
Revises: 086e8c9716c3
Create Date: 2026-10-18 18:48:57.639790

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a8da3b2d2f4"
down_revision: Union[str, Sequence[str], None] = "086e8c9716c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

message_sender = sa.Enum("customer", "agent", "bot", name="messagesender")


def upgrade() -> None:
    # 1. Expand: add the new tables and ticket columns. customer_id allows empty values for
    #    now, because the tickets already in the table don't have one yet.
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("phone", name=op.f("uq_customers_phone")),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("email", name=op.f("uq_agents_email")),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("sender", message_sender, nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_messages_ticket_id_tickets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(op.f("ix_messages_ticket_id"), "messages", ["ticket_id"])
    op.add_column("tickets", sa.Column("customer_id", sa.Integer(), nullable=True))
    op.add_column("tickets", sa.Column("agent_id", sa.Integer(), nullable=True))

    # 2. Backfill: copy the existing data into the new tables.
    # One customer per phone number, with the name and time of their earliest ticket.
    op.execute(
        """
        INSERT INTO customers (name, phone, created_at)
        SELECT DISTINCT ON (phone) customer_name, phone, created_at
        FROM tickets
        ORDER BY phone, created_at, id
        """
    )
    op.execute(
        """
        UPDATE tickets SET customer_id = customers.id
        FROM customers
        WHERE customers.phone = tickets.phone
        """
    )
    # Each ticket's message becomes the first message in its conversation.
    op.execute(
        """
        INSERT INTO messages (ticket_id, sender, body, created_at)
        SELECT id, 'customer', message, created_at
        FROM tickets
        ORDER BY id
        """
    )

    # 3. Contract: every ticket has a customer now, so require one, link the tables, and drop
    #    the old columns.
    op.alter_column("tickets", "customer_id", nullable=False)
    op.create_index(op.f("ix_tickets_customer_id"), "tickets", ["customer_id"])
    op.create_index(op.f("ix_tickets_agent_id"), "tickets", ["agent_id"])
    op.create_foreign_key(
        op.f("fk_tickets_customer_id_customers"), "tickets", "customers", ["customer_id"], ["id"]
    )
    op.create_foreign_key(
        op.f("fk_tickets_agent_id_agents"), "tickets", "agents", ["agent_id"], ["id"]
    )
    op.drop_column("tickets", "customer_name")
    op.drop_column("tickets", "phone")
    op.drop_column("tickets", "message")


def downgrade() -> None:
    # Put each ticket's customer and first message back on the ticket. Agents, assignments,
    # and every message after the first are lost: the old schema has nowhere to keep them.
    op.add_column("tickets", sa.Column("customer_name", sa.String(length=100), nullable=True))
    op.add_column("tickets", sa.Column("phone", sa.String(length=20), nullable=True))
    op.add_column("tickets", sa.Column("message", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE tickets SET customer_name = customers.name, phone = customers.phone
        FROM customers
        WHERE customers.id = tickets.customer_id
        """
    )
    op.execute(
        """
        UPDATE tickets SET message = first_message.body
        FROM (
            SELECT DISTINCT ON (ticket_id) ticket_id, body
            FROM messages
            ORDER BY ticket_id, created_at, id
        ) AS first_message
        WHERE first_message.ticket_id = tickets.id
        """
    )
    # The old message column can't be empty, so a ticket without messages gets "".
    op.execute("UPDATE tickets SET message = '' WHERE message IS NULL")
    for column in ["customer_name", "phone", "message"]:
        op.alter_column("tickets", column, nullable=False)

    op.drop_constraint(op.f("fk_tickets_agent_id_agents"), "tickets", type_="foreignkey")
    op.drop_constraint(op.f("fk_tickets_customer_id_customers"), "tickets", type_="foreignkey")
    op.drop_index(op.f("ix_tickets_agent_id"), table_name="tickets")
    op.drop_index(op.f("ix_tickets_customer_id"), table_name="tickets")
    op.drop_column("tickets", "agent_id")
    op.drop_column("tickets", "customer_id")
    op.drop_index(op.f("ix_messages_ticket_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_table("agents")
    op.drop_table("customers")
    message_sender.drop(op.get_bind())
