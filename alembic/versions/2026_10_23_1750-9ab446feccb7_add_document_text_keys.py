"""Add where each document's text is

The Lambda saves the text it pulls out of each PDF to S3, and the row records where, for the
Stage 6 worker to read. Empty until the document is processed, so existing rows stay valid.

Revision ID: 9ab446feccb7
Revises: 0971a3287aac
Create Date: 2026-10-23 17:50:32.555295

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9ab446feccb7"
down_revision: str | Sequence[str] | None = "0971a3287aac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("text_s3_key", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "text_s3_key")
