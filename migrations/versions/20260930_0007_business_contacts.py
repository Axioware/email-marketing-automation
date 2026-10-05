"""Create business contacts.

Revision ID: 20260930_0007
Revises: 20260930_0006
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260930_0007"
down_revision = "20260930_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "business_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Integer(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE", name="fk_business_contacts_business"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255)),
        sa.Column("first_name", sa.String(120)),
        sa.Column("last_name", sa.String(120)),
        sa.Column("job_title", sa.String(255)),
        sa.Column("role_type", sa.String(64)),
        sa.Column("email", sa.String(320)),
        sa.Column("email_source", sa.String(32)),
        sa.Column("email_status", sa.String(32), server_default="unverified"),
        sa.Column("linkedin_url", sa.Text()),
        sa.Column(
            "source_urls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("confidence", sa.Numeric(4, 3)),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("discovery_reasoning", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_business_contacts_confidence_range",
        ),
    )
    op.create_index("ix_business_contacts_business_id", "business_contacts", ["business_id"])
    op.create_index("ix_business_contacts_email", "business_contacts", ["email"])
    op.execute(
        """
        CREATE FUNCTION set_business_contacts_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_business_contacts_updated_at
        BEFORE UPDATE ON business_contacts
        FOR EACH ROW
        EXECUTE FUNCTION set_business_contacts_updated_at()
        """
    )


def downgrade():
    op.execute("DROP TRIGGER trg_business_contacts_updated_at ON business_contacts")
    op.execute("DROP FUNCTION set_business_contacts_updated_at()")
    op.drop_index("ix_business_contacts_email", table_name="business_contacts")
    op.drop_index("ix_business_contacts_business_id", table_name="business_contacts")
    op.drop_table("business_contacts")