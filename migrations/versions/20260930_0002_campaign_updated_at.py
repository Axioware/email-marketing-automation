"""Add automatic updated_at tracking to discovery campaigns.

Revision ID: 20260930_0002
Revises: 20260930_0001
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "20260930_0002"
down_revision = "20260930_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "discovery_campaigns",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        """
        CREATE FUNCTION set_discovery_campaigns_updated_at()
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
        CREATE TRIGGER trg_discovery_campaigns_updated_at
        BEFORE UPDATE ON discovery_campaigns
        FOR EACH ROW
        EXECUTE FUNCTION set_discovery_campaigns_updated_at()
        """
    )


def downgrade():
    op.execute("DROP TRIGGER trg_discovery_campaigns_updated_at ON discovery_campaigns")
    op.execute("DROP FUNCTION set_discovery_campaigns_updated_at()")
    op.drop_column("discovery_campaigns", "updated_at")