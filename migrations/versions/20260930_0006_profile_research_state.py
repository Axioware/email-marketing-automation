"""Persist compact website research state.

Revision ID: 20260930_0006
Revises: 20260930_0005
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260930_0006"
down_revision = "20260930_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "business_website_profiles",
        sa.Column(
            "scraped_pages",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "business_website_profiles",
        sa.Column(
            "discovered_links",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("business_website_profiles", "discovered_links")
    op.drop_column("business_website_profiles", "scraped_pages")