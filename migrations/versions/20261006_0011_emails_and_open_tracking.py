"""Create emails and email open tracking.

Revision ID: 20261006_0011
Revises: 20261005_0010
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261006_0011"
down_revision = "20261005_0010"
branch_labels = None
depends_on = None

FOOTER_BUCKET = "email-assets"


def upgrade():
    op.create_table(
        "emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "prospect_id",
            sa.Integer(),
            sa.ForeignKey("prospects.id", ondelete="CASCADE", name="fk_emails_prospect"),
            nullable=False,
        ),
        sa.Column(
            "business_id",
            sa.Integer(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE", name="fk_emails_business"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("business_contacts.id", ondelete="CASCADE", name="fk_emails_contact"),
            nullable=False,
        ),
        sa.Column("sequence_step", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("tracking_token", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("generation_provider", sa.String(32)),
        sa.Column("generation_model", sa.String(128)),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("sent_from", sa.String(320)),
        sa.Column("message_id", sa.String(255)),
        sa.Column("send_error", sa.Text()),
        sa.Column("first_opened_at", sa.DateTime(timezone=True)),
        sa.Column("last_opened_at", sa.DateTime(timezone=True)),
        sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tracking_token", name="uq_emails_tracking_token"),
        sa.UniqueConstraint("prospect_id", "sequence_step", name="uq_emails_prospect_step"),
        sa.CheckConstraint(
            "status IN ('draft', 'sending', 'sent', 'opened', 'failed')",
            name="ck_emails_status",
        ),
    )
    op.create_index("ix_emails_business_id", "emails", ["business_id"])
    op.create_index("ix_emails_status", "emails", ["status"])

    op.create_table(
        "email_open_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="CASCADE", name="fk_email_open_events_email"),
            nullable=False,
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_agent", sa.Text()),
    )
    op.create_index("ix_email_open_events_email_id", "email_open_events", ["email_id"])

    op.execute(
        """
        CREATE FUNCTION set_emails_updated_at()
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
        CREATE TRIGGER trg_emails_updated_at
        BEFORE UPDATE ON emails
        FOR EACH ROW
        EXECUTE FUNCTION set_emails_updated_at()
        """
    )

    # Called by the tracking edge function. One atomic statement records the open, so concurrent image requests
    # cannot lose counts. Only emails that were actually sent count: an image request for a draft (e.g. a preview)
    # is ignored. Returns whether an open was recorded.
    op.execute(
        """
        CREATE FUNCTION record_email_open(p_token text, p_user_agent text DEFAULT NULL)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_email_id integer;
        BEGIN
            UPDATE emails
               SET first_opened_at = COALESCE(first_opened_at, now()),
                   last_opened_at = now(),
                   open_count = open_count + 1,
                   status = CASE WHEN status = 'sent' THEN 'opened' ELSE status END
             WHERE tracking_token = p_token
               AND sent_at IS NOT NULL
            RETURNING id INTO v_email_id;

            IF v_email_id IS NULL THEN
                RETURN false;
            END IF;

            INSERT INTO email_open_events (email_id, user_agent)
            VALUES (v_email_id, left(p_user_agent, 500));
            RETURN true;
        END;
        $$
        """
    )

    # Supabase exposes the public schema through its REST API. Row level security with no policies keeps these
    # tables and the function private: only the service role (the edge function and this backend) can use them.
    op.execute("ALTER TABLE emails ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_open_events ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON FUNCTION record_email_open(text, text) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON FUNCTION record_email_open(text, text) FROM anon, authenticated;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
                GRANT EXECUTE ON FUNCTION record_email_open(text, text) TO service_role;
            END IF;
        END
        $$
        """
    )

    # Public Supabase Storage bucket for the footer image the tracking function returns. Skipped outside Supabase.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'storage' AND table_name = 'buckets') THEN
                INSERT INTO storage.buckets (id, name, public)
                VALUES ('{FOOTER_BUCKET}', '{FOOTER_BUCKET}', true)
                ON CONFLICT (id) DO NOTHING;
            END IF;
        END
        $$
        """
    )


def downgrade():
    # The storage bucket is left in place: it may hold the footer image, and Supabase refuses to drop non-empty buckets.
    op.execute("DROP FUNCTION record_email_open(text, text)")
    op.execute("DROP TRIGGER trg_emails_updated_at ON emails")
    op.execute("DROP FUNCTION set_emails_updated_at()")
    op.drop_index("ix_email_open_events_email_id", table_name="email_open_events")
    op.drop_table("email_open_events")
    op.drop_index("ix_emails_status", table_name="emails")
    op.drop_index("ix_emails_business_id", table_name="emails")
    op.drop_table("emails")
