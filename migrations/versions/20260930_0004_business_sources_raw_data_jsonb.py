"""Store business source payloads as PostgreSQL JSONB.

Revision ID: 20260930_0004
Revises: 20260930_0003
Create Date: 2026-09-30
"""

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260930_0004"
down_revision = "20260930_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "business_sources",
        "raw_data",
        existing_type=postgresql.JSON(),
        type_=postgresql.JSONB(),
        postgresql_using="raw_data::jsonb",
    )


def downgrade():
    op.alter_column(
        "business_sources",
        "raw_data",
        existing_type=postgresql.JSONB(),
        type_=postgresql.JSON(),
        postgresql_using="raw_data::json",
    )