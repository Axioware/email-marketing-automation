from django.apps import AppConfig
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models.signals import post_migrate


def enable_row_level_security(using=DEFAULT_DB_ALIAS, **kwargs):
    """On Supabase, turn on row level security for every table in the public schema, Django's own included.

    Supabase publishes the public schema through its REST API to anyone holding the project's anon key. With RLS on
    and no policies, only the database owner (this app) and the service role (the tracking edge function) can read
    or write, so tables such as auth_user or authtoken_token are never exposed. Skipped outside Supabase.
    """
    connection = connections[using]
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = 'anon'")
        if cursor.fetchone() is None:
            return
        cursor.execute(
            """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity
            """
        )
        for (table,) in cursor.fetchall():
            cursor.execute(f"ALTER TABLE public.{connection.ops.quote_name(table)} ENABLE ROW LEVEL SECURITY")


class PipelineConfig(AppConfig):
    name = "pipeline"
    verbose_name = "Outreach pipeline"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        post_migrate.connect(enable_row_level_security, sender=self, dispatch_uid="pipeline-enable-rls")

