"""Add the predictions table

Each row is what the priority or sentiment model said about a new ticket, which the worker
saves as data for retraining. The table is new, so no existing rows need values.

Revision ID: 5e2b7c9d41a6
Revises: d2f70275da52
Create Date: 2026-10-24 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e2b7c9d41a6"
down_revision: str | Sequence[str] | None = "d2f70275da52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from app.models, so this migration keeps doing the same
# thing after the models change.
model_name = sa.Enum("priority", "sentiment", name="modelname")


def upgrade() -> None:
    # Creating the table also creates the enum type its model column uses.
    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("model", model_name, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=20), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_predictions_ticket_id_tickets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_predictions")),
    )
    op.create_index(op.f("ix_predictions_ticket_id"), "predictions", ["ticket_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_predictions_ticket_id"), table_name="predictions")
    op.drop_table("predictions")
    # Dropping the table leaves its enum type behind.
    model_name.drop(op.get_bind())
