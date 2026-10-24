"""Add the chunks table

Each row is a piece of a help doc's text and its embedding, which the Stage 6 worker saves
and the bot searches. The table is new, so no existing rows need values.

Revision ID: d2f70275da52
Revises: c22b4b22ed21
Create Date: 2026-10-23 22:17:48.564910

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "d2f70275da52"
down_revision: str | Sequence[str] | None = "c22b4b22ed21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        # 1,536 numbers: text-embedding-3-small's size, in app/models.py.
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint("document_id", "position", name=op.f("uq_chunks_document_id")),
    )


def downgrade() -> None:
    op.drop_table("chunks")
