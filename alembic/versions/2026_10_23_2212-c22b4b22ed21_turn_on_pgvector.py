"""Turn on pgvector

pgvector adds the vector type, for the embeddings of help-doc chunks, and nearest-neighbour
search over them. Creating it needs a superuser, because it loads C code into the database:
RDS's master user has rds_superuser, and the compose and CI containers' user is a superuser.

Revision ID: c22b4b22ed21
Revises: 9ab446feccb7
Create Date: 2026-10-23 22:12:26.751128

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c22b4b22ed21"
down_revision: str | Sequence[str] | None = "9ab446feccb7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS, so a database where someone already turned it on by hand still upgrades.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
