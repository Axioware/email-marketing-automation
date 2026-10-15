import sys
import time

from django.core.management.base import BaseCommand, CommandError


class ServiceCommand(BaseCommand):
    """A management command that runs one pipeline service module (its `add_arguments` and `run`).

    The web app on Vercel also passes `deadline` (time.monotonic() by which the step must be done, so the request
    answers within Vercel's time limit; pipeline/webruns.py). It is not a command-line option: steps check it with
    out_of_time() before starting new work and say what is left for the next run.
    """

    service = None
    stealth_options = ("deadline",)

    def add_arguments(self, parser):
        self.service.add_arguments(parser)

    def handle(self, *args, **options):
        code = self.service.run(options)
        sys.stdout.flush()
        if code:
            raise CommandError("Finished with errors (see the output above).", returncode=code)


def out_of_time(options: dict, reserve: float = 0.0) -> bool:
    """Whether less than `reserve` seconds are left before the run's deadline (never, without one)."""
    deadline = options.get("deadline")
    return deadline is not None and time.monotonic() > deadline - reserve


def time_limit_note(left: int, what: str) -> None:
    print(f"Time limit reached: {left} {what} not done yet; start the run again to continue (finished work is "
          "skipped).", flush=True)
