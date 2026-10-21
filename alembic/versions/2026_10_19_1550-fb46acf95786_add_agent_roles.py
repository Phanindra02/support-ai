"""Add agent roles

Every agent who's already in the table becomes a plain agent. The first admin is made with
scripts/create_agent.py --admin.

Revision ID: fb46acf95786
Revises: 89711c13eabd
Create Date: 2026-10-19 15:50:14.706039

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "fb46acf95786"
down_revision: str | Sequence[str] | None = "89711c13eabd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False, because the type is created and dropped below, by hand. Adding or
# dropping a column doesn't do that by itself, and a type left behind would make the
# next upgrade fail with "type already exists".
agent_role = postgresql.ENUM("agent", "admin", name="agentrole", create_type=False)


def upgrade() -> None:
    agent_role.create(op.get_bind())
    op.add_column(
        "agents", sa.Column("role", agent_role, server_default="agent", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("agents", "role")
    agent_role.drop(op.get_bind())
