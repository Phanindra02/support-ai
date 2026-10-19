"""Add agent passwords and an active flag

Agents who are already in the table get no password, so they can't log in until one is set
with scripts/create_agent.py, and they start out active.

Revision ID: 89711c13eabd
Revises: a8dd85a3d297
Create Date: 2026-10-19 15:45:33.652779

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "89711c13eabd"
down_revision: Union[str, Sequence[str], None] = "a8dd85a3d297"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("password_hash", sa.String(length=255), nullable=True))
    # Adding a column with a constant default doesn't rewrite the table in Postgres 11 and
    # later, so this is quick however many agents there are.
    op.add_column(
        "agents",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("agents", "is_active")
    op.drop_column("agents", "password_hash")
