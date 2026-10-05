"""Add email verification results to business contacts.

Revision ID: 20261001_0009
Revises: 20261001_0008
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261001_0009"
down_revision = "20261001_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("business_contacts", sa.Column("email_checked_at", sa.DateTime(timezone=True)))
    op.add_column("business_contacts", sa.Column("email_check_details", postgresql.JSONB()))


def downgrade():
    op.drop_column("business_contacts", "email_check_details")
    op.drop_column("business_contacts", "email_checked_at")
