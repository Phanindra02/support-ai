"""Create the tickets table

Revision ID: 086e8c9716c3
Revises:
Create Date: 2026-10-18 18:41:55.839196

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "086e8c9716c3"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values are written out instead of imported from app.models. A migration is a snapshot
# of one moment, and it must keep doing the same thing after the models change.
ticket_status = sa.Enum("open", "pending", "closed", name="ticketstatus")
ticket_priority = sa.Enum("low", "medium", "high", "urgent", name="ticketpriority")


def upgrade() -> None:
    # Creating the table also creates the two enum types its columns use.
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", ticket_status, nullable=False),
        sa.Column("priority", ticket_priority, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )


def downgrade() -> None:
    op.drop_table("tickets")
    # Dropping a table leaves its enum types behind, and autogenerate doesn't drop them.
    # Without this, upgrading again would fail with "type ticketstatus already exists".
    ticket_priority.drop(op.get_bind())
    ticket_status.drop(op.get_bind())
