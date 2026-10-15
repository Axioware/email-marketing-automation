"""Create discovery campaigns and business tables.

Revision ID: 20260930_0001
Revises:
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "20260930_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discovery_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("target_country", sa.String(100)),
        sa.Column("target_locations", sa.JSON()),
        sa.Column("search_terms", sa.JSON()),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_discovery_campaigns_status",
        ),
    )
    op.create_index("ix_discovery_campaigns_status", "discovery_campaigns", ["status"])

    op.create_table(
        "businesses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "discovery_campaign_id",
            sa.Integer(),
            sa.ForeignKey("discovery_campaigns.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(255)),
        sa.Column("website_url", sa.Text()),
        sa.Column("domain", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("address", sa.Text()),
        sa.Column("city", sa.String(120)),
        sa.Column("state", sa.String(120)),
        sa.Column("country", sa.String(100)),
        sa.Column("postal_code", sa.String(30)),
        sa.Column("latitude", sa.Numeric(10, 7)),
        sa.Column("longitude", sa.Numeric(10, 7)),
        sa.Column("google_place_id", sa.String(255)),
        sa.Column("google_maps_url", sa.Text()),
        sa.Column("google_rating", sa.Numeric(2, 1)),
        sa.Column("google_review_count", sa.Integer()),
        sa.Column("opening_hours", sa.JSON()),
        sa.Column("source", sa.String(100)),
        sa.Column("source_url", sa.Text()),
        sa.Column("first_discovered_at", sa.DateTime(timezone=True)),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_businesses_discovery_campaign_id", "businesses", ["discovery_campaign_id"]
    )
    op.create_index("ix_businesses_google_place_id", "businesses", ["google_place_id"])

    op.create_table(
        "business_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Integer(),
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_business_id", sa.String(255)),
        sa.Column("source_url", sa.Text()),
        sa.Column("raw_data", sa.JSON()),
        sa.Column(
            "discovered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_business_sources_business_id", "business_sources", ["business_id"])


def downgrade():
    op.drop_index("ix_business_sources_business_id", table_name="business_sources")
    op.drop_table("business_sources")
    op.drop_index("ix_businesses_google_place_id", table_name="businesses")
    op.drop_index("ix_businesses_discovery_campaign_id", table_name="businesses")
    op.drop_table("businesses")
    op.drop_index("ix_discovery_campaigns_status", table_name="discovery_campaigns")
    op.drop_table("discovery_campaigns")