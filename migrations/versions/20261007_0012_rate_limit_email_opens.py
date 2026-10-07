"""Rate limit recorded email opens in the database.

The edge function's in-memory limiter is per instance, and Supabase spreads requests over many instances, so it
cannot enforce a limit on its own. record_email_open now counts at most OPENS_PER_MINUTE opens per email per
minute. It locks the email row first, so concurrent requests are counted exactly.

Revision ID: 20261007_0012
Revises: 20261006_0011
Create Date: 2026-10-07
"""

from alembic import op

revision = "20261007_0012"
down_revision = "20261006_0011"
branch_labels = None
depends_on = None

OPENS_PER_MINUTE = 10


def upgrade():
    op.create_index("ix_email_open_events_email_id_opened_at", "email_open_events", ["email_id", "opened_at"])
    op.drop_index("ix_email_open_events_email_id", table_name="email_open_events")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION record_email_open(p_token text, p_user_agent text DEFAULT NULL)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_email_id integer;
            v_recent integer;
        BEGIN
            -- Only sent emails count; the row lock serialises concurrent opens of the same email.
            SELECT id INTO v_email_id
              FROM emails
             WHERE tracking_token = p_token
               AND sent_at IS NOT NULL
               FOR UPDATE;
            IF v_email_id IS NULL THEN
                RETURN false;
            END IF;

            SELECT count(*) INTO v_recent
              FROM email_open_events
             WHERE email_id = v_email_id
               AND opened_at > now() - interval '1 minute';
            IF v_recent >= {OPENS_PER_MINUTE} THEN
                RETURN false;  -- over the limit: the image is still served, the open is not recorded
            END IF;

            UPDATE emails
               SET first_opened_at = COALESCE(first_opened_at, now()),
                   last_opened_at = now(),
                   open_count = open_count + 1,
                   status = CASE WHEN status = 'sent' THEN 'opened' ELSE status END
             WHERE id = v_email_id;

            INSERT INTO email_open_events (email_id, user_agent)
            VALUES (v_email_id, left(p_user_agent, 500));
            RETURN true;
        END;
        $$
        """
    )


def downgrade():
    op.execute(
        """
        CREATE OR REPLACE FUNCTION record_email_open(p_token text, p_user_agent text DEFAULT NULL)
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
    op.create_index("ix_email_open_events_email_id", "email_open_events", ["email_id"])
    op.drop_index("ix_email_open_events_email_id_opened_at", table_name="email_open_events")
