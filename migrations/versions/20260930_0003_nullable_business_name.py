"""Allow business records without an available name.

Revision ID: 20260930_0003
Revises: 20260930_0002
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "20260930_0003"
down_revision = "20260930_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "businesses",
        "name",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        "businesses",
        "name",
        existing_type=sa.String(length=255),
        nullable=False,
    )