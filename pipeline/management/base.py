import sys

from django.core.management.base import BaseCommand, CommandError


class ServiceCommand(BaseCommand):
    """A management command that runs one pipeline service module (its `add_arguments` and `run`)."""

    service = None

    def add_arguments(self, parser):
        self.service.add_arguments(parser)

    def handle(self, *args, **options):
        code = self.service.run(options)
        sys.stdout.flush()
        if code:
            raise CommandError("Finished with errors (see the output above).", returncode=code)
