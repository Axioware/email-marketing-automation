import sys

from django.core.management.base import BaseCommand, CommandError

from pipeline.services import maps


class Command(BaseCommand):
    help = ("Module 1: fill in missing Google Maps details (category, review count, rating, hours) and city, "
            "state and postal code from the address. No AI.")

    def add_arguments(self, parser):
        maps.add_refresh_arguments(parser)

    def handle(self, *args, **options):
        code = maps.run_refresh(options)
        sys.stdout.flush()
        if code:
            raise CommandError("Finished with errors (see the output above).", returncode=code)
