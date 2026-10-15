"""Run queued pipeline runs on this machine (RUN_MODE=queue: the web app on Vercel only stores runs).

Start it on the machine that has the browsers (Chromium, Firefox) and Docker (Reacher, the scraper container), with
the same .env as the web app (DATABASE_URL etc.) but without RUN_MODE=queue:

    python manage.py run_worker            # keeps running; one run at a time

It reports in every 30 seconds (the dashboard shows when it was last seen), stops a run when someone presses Stop,
and on start marks runs it was running before a restart as failed.
"""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from pipeline import jobs
from pipeline.models import PipelineRun, WorkerHeartbeat

HEARTBEAT_SECONDS = 30


def heartbeat(name: str) -> None:
    from pipeline.services import scraper_api

    problem = scraper_api.status(timeout=2)
    WorkerHeartbeat.objects.update_or_create(
        name=name, defaults={"last_seen": timezone.now(), "info": {
            "scraper": "running" if problem is None else problem, "scraper_url": scraper_api.base_url(),
            "pid": os.getpid()}})


class Command(BaseCommand):
    help = "Run queued pipeline runs on this machine (for a web app on Vercel with RUN_MODE=queue)."

    def add_arguments(self, parser):
        parser.add_argument("--poll", type=float, default=5.0, help="Seconds between looks for new runs (default: 5).")
        parser.add_argument("--once", action="store_true", help="Run what is queued now, then exit.")

    def handle(self, *args, **options):
        name = f"{socket.gethostname()}"
        jobs.fail_dead_runs()  # runs this machine was executing before a restart
        self.stdout.write(f"Worker {name} started; waiting for queued runs (Ctrl+C to stop).")
        last_beat = 0.0
        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        try:
            while not stop["now"]:
                close_old_connections()
                clock = time.monotonic()
                if clock - last_beat >= HEARTBEAT_SECONDS:
                    heartbeat(name)
                    last_beat = clock
                run = (PipelineRun.objects.filter(status=PipelineRun.Status.QUEUED, runs_on="worker")
                       .order_by("created_at", "id").first())
                if run is None:
                    if options["once"]:
                        return
                    time.sleep(options["poll"])
                    continue
                self.run_one(run, name)
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")

    def run_one(self, run: PipelineRun, name: str) -> None:
        self.stdout.write(f"Run #{run.pk}: {run.command_line}")
        with open(jobs.log_path(run.pk), "ab") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "run_pipeline_job", str(run.pk)],
                cwd=settings.BASE_DIR, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "RUN_MODE": "local"},
            )
        last_beat = time.monotonic()
        while process.poll() is None:
            time.sleep(2)
            close_old_connections()
            if PipelineRun.objects.filter(pk=run.pk, cancel_requested=True).exists():
                process.send_signal(signal.SIGTERM)
            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                heartbeat(name)
                last_beat = time.monotonic()
        jobs.fail_dead_runs()  # the run's process ended without recording a result (crash): mark it failed
        run.refresh_from_db()
        self.stdout.write(f"Run #{run.pk} {run.status}.")
