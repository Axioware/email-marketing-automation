"""Database-side behaviour Django models cannot express: updated_at triggers, the open-tracking function with its
rate limit, row level security and the Supabase Storage bucket for the footer image.

Every statement is idempotent. On a database created before the move to Django (adopted with
`migrate --fake-initial`), this re-applies the same definitions and converts the three remaining `json` columns to
`jsonb`; on a new database it creates them.
"""
from django.db import migrations

OPENS_PER_MINUTE = 10  # opens recorded per email per minute; extra image loads are served but not counted
FOOTER_BUCKET = "email-assets"
UPDATED_AT_TABLES = ("discovery_campaigns", "business_website_profiles", "business_contacts", "prospects", "emails")
JSON_COLUMNS = (("discovery_campaigns", "target_locations"), ("discovery_campaigns", "search_terms"),
                ("businesses", "opening_hours"))

# Column defaults the database itself applies (Django only applies its defaults in Python), so rows inserted with
# plain SQL, e.g. from the Supabase SQL editor, get the same values as rows created through Django.
NOW = "now()"
EMPTY = "'[]'::jsonb"
COLUMN_DEFAULTS = {
    "discovery_campaigns": {"status": "'pending'", "created_at": NOW, "updated_at": NOW},
    "businesses": {"created_at": NOW, "updated_at": NOW},
    "business_sources": {"discovered_at": NOW},
    "business_website_profiles": {"status": "'pending'", "pages_scraped": "0", "scraped_urls": EMPTY,
                                  "scraped_pages": EMPTY, "discovered_links": EMPTY, "emails": EMPTY,
                                  "qualification_reasons": EMPTY, "created_at": NOW, "updated_at": NOW},
    "business_contacts": {"email_status": "'unverified'", "source_urls": EMPTY, "candidate_emails": EMPTY,
                          "is_primary": "false", "created_at": NOW, "updated_at": NOW},
    "prospects": {"outreach_status": "'pending'", "outreach_facts": EMPTY, "do_not_contact": "false",
                  "created_at": NOW, "updated_at": NOW},
    "emails": {"sequence_step": "1", "status": "'in_review'", "open_count": "0", "created_at": NOW, "updated_at": NOW},
}


def updated_at_trigger(table: str) -> list[str]:
    return [
        f"""
        CREATE OR REPLACE FUNCTION set_{table}_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}",
        f"""
        CREATE TRIGGER trg_{table}_updated_at
        BEFORE UPDATE ON {table}
        FOR EACH ROW
        EXECUTE FUNCTION set_{table}_updated_at()
        """,
    ]


# Called by the tracking edge function. Only sent emails count; the row lock serialises concurrent opens of the
# same email, and at most OPENS_PER_MINUTE opens per email per minute are recorded. Returns whether it counted.
RECORD_EMAIL_OPEN = f"""
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

FUNCTION_PRIVILEGES = """
DO $$
BEGIN
    REVOKE ALL ON FUNCTION record_email_open(text, text) FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION record_email_open(text, text) FROM anon, authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION record_email_open(text, text) TO service_role;
    END IF;
END
$$
"""

# Public Supabase Storage bucket for the footer image the tracking function returns. Skipped outside Supabase.
STORAGE_BUCKET = f"""
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

FORWARD = [
    *(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb" for table, column in JSON_COLUMNS),
    *(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default}"
      for table, columns in COLUMN_DEFAULTS.items() for column, default in columns.items()),
    *(statement for table in UPDATED_AT_TABLES for statement in updated_at_trigger(table)),
    "CREATE INDEX IF NOT EXISTS ix_email_open_events_email_id_opened_at ON email_open_events (email_id, opened_at)",
    "DROP INDEX IF EXISTS ix_email_open_events_email_id",  # superseded by the index above
    RECORD_EMAIL_OPEN,
    FUNCTION_PRIVILEGES,
    # Supabase exposes the public schema through its REST API. RLS without policies keeps the tables private to
    # the database owner and the service role. (apps.py turns it on for every other table on Supabase.)
    "ALTER TABLE emails ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE email_open_events ENABLE ROW LEVEL SECURITY",
    STORAGE_BUCKET,
]

BACKWARD = [
    "DROP FUNCTION IF EXISTS record_email_open(text, text)",
    "DROP INDEX IF EXISTS ix_email_open_events_email_id_opened_at",
    *(statement for table in UPDATED_AT_TABLES for statement in (
        f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}",
        f"DROP FUNCTION IF EXISTS set_{table}_updated_at()",
    )),
]


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=BACKWARD),
    ]
