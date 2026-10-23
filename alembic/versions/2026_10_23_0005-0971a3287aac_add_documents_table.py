"""Add the documents table

One row per help doc an agent uploads. The PDF itself is in S3, and the row says where.

Revision ID: 0971a3287aac
Revises: 54c6c062563f
Create Date: 2026-10-23 00:05:06.676447

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0971a3287aac"
down_revision: str | Sequence[str] | None = "54c6c062563f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from app.models, so this migration keeps doing the same
# thing after the models change.
document_status = sa.Enum("uploaded", "processed", name="documentstatus")


def upgrade() -> None:
    # Creating the table also creates the enum type its status column uses.
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("s3_key", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", document_status, nullable=False),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"], ["agents.id"], name=op.f("fk_documents_uploaded_by_id_agents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("s3_key", name=op.f("uq_documents_s3_key")),
    )


def downgrade() -> None:
    op.drop_table("documents")
    # Dropping a table leaves its enum type behind, and autogenerate doesn't drop it.
    document_status.drop(op.get_bind())
