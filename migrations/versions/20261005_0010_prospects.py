"""Create prospects.

Revision ID: 20261005_0010
Revises: 20261001_0009
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261005_0010"
down_revision = "20261001_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "prospects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Integer(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE", name="fk_prospects_business"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("business_contacts.id", ondelete="CASCADE", name="fk_prospects_contact"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_status", sa.String(32)),
        sa.Column("email_verification_provider", sa.String(64)),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("qualification_score", sa.Integer()),
        sa.Column("outreach_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("outreach_priority", sa.String(16)),
        sa.Column(
            "outreach_facts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("research_summary", sa.Text()),
        sa.Column("do_not_contact", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_contacted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("contact_id", "email", name="uq_prospects_contact_email"),
    )
    op.create_index("ix_prospects_business_id", "prospects", ["business_id"])
    op.create_index("ix_prospects_email", "prospects", ["email"])
    op.create_index("ix_prospects_email_status", "prospects", ["email_status"])
    op.create_index("ix_prospects_outreach_status", "prospects", ["outreach_status"])
    op.execute(
        """
        CREATE FUNCTION set_prospects_updated_at()
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
        CREATE TRIGGER trg_prospects_updated_at
        BEFORE UPDATE ON prospects
        FOR EACH ROW
        EXECUTE FUNCTION set_prospects_updated_at()
        """
    )


def downgrade():
    op.execute("DROP TRIGGER trg_prospects_updated_at ON prospects")
    op.execute("DROP FUNCTION set_prospects_updated_at()")
    op.drop_index("ix_prospects_outreach_status", table_name="prospects")
    op.drop_index("ix_prospects_email_status", table_name="prospects")
    op.drop_index("ix_prospects_email", table_name="prospects")
    op.drop_index("ix_prospects_business_id", table_name="prospects")
    op.drop_table("prospects")
