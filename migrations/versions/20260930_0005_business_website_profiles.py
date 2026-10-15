"""Create business website profiles.

Revision ID: 20260930_0005
Revises: 20260930_0004
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260930_0005"
down_revision = "20260930_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "business_website_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Integer(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE", name="fk_website_profiles_business"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("pages_scraped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "scraped_urls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "emails",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("qualification_score", sa.Integer()),
        sa.Column(
            "qualification_reasons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("agent_reasoning", sa.Text()),
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
        sa.UniqueConstraint(
            "business_id",
            name="uq_business_website_profiles_business_id",
        ),
    )
    op.create_index(
        "ix_business_website_profiles_status",
        "business_website_profiles",
        ["status"],
    )
    op.execute(
        """
        CREATE FUNCTION set_business_website_profiles_updated_at()
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
        CREATE TRIGGER trg_business_website_profiles_updated_at
        BEFORE UPDATE ON business_website_profiles
        FOR EACH ROW
        EXECUTE FUNCTION set_business_website_profiles_updated_at()
        """
    )


def downgrade():
    op.execute(
        "DROP TRIGGER trg_business_website_profiles_updated_at ON business_website_profiles"
    )
    op.execute("DROP FUNCTION set_business_website_profiles_updated_at()")
    op.drop_index(
        "ix_business_website_profiles_status",
        table_name="business_website_profiles",
    )
    op.drop_table("business_website_profiles")