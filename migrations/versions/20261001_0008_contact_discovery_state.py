"""Add candidate emails and contact discovery state.

Revision ID: 20261001_0008
Revises: 20260930_0007
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261001_0008"
down_revision = "20260930_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "business_contacts",
        sa.Column(
            "candidate_emails",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "business_website_profiles",
        sa.Column("contact_discovery_status", sa.String(32)),
    )
    op.add_column(
        "business_website_profiles",
        sa.Column("contact_discovery_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_business_website_profiles_contact_discovery_status",
        "business_website_profiles",
        ["contact_discovery_status"],
    )


def downgrade():
    op.drop_index(
        "ix_business_website_profiles_contact_discovery_status",
        table_name="business_website_profiles",
    )
    op.drop_column("business_website_profiles", "contact_discovery_at")
    op.drop_column("business_website_profiles", "contact_discovery_status")
    op.drop_column("business_contacts", "candidate_emails")
