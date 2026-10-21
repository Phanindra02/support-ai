"""Index tickets by status and created time

An index on priority was tried and measured too, and Postgres never used it, so it's left
out. See docs/results.md.

Revision ID: a8dd85a3d297
Revises: 5a8da3b2d2f4
Create Date: 2026-10-18 18:58:22.986683

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8dd85a3d297"
down_revision: str | Sequence[str] | None = "5a8da3b2d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain CREATE INDEX blocks writes to the table while it builds. On 100,000 rows that
    # takes well under a second. On a big live table, CREATE INDEX CONCURRENTLY would build
    # it without blocking writes, but it can't run inside a transaction like this one.
    op.create_index("ix_tickets_created_at_id", "tickets", ["created_at", "id"])
    op.create_index(op.f("ix_tickets_status"), "tickets", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_tickets_status"), table_name="tickets")
    op.drop_index("ix_tickets_created_at_id", table_name="tickets")
