from django.core.management.base import BaseCommand

from pipeline.jobs import execute_run


class Command(BaseCommand):
    help = "Internal: execute a queued pipeline run (started from the admin or the API)."

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)

    def handle(self, *args, **options):
        execute_run(options["run_id"])
