"""Add Twilio message ids

Each WhatsApp message keeps Twilio's id for it, so a message Twilio delivers twice is only
stored once. Messages already in the table didn't come with one, so they're left empty.

Revision ID: 54c6c062563f
Revises: fb46acf95786
Create Date: 2026-10-20 22:03:28.592765

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "54c6c062563f"
down_revision: str | Sequence[str] | None = "fb46acf95786"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("twilio_sid", sa.String(length=34), nullable=True))
    # The unique constraint also gives the column the index that finding a repeat needs.
    op.create_unique_constraint(op.f("uq_messages_twilio_sid"), "messages", ["twilio_sid"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_messages_twilio_sid"), "messages", type_="unique")
    op.drop_column("messages", "twilio_sid")
