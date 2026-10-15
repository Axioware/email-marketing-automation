"""On Vercel every run executes inside the request that starts it: no worker, no queue, nothing running elsewhere.

The request runs the command, which calls your server and outside services directly (the scraper container and Reacher
behind nginx, SearXNG, Jina Reader, the AI, GoHighLevel), saves the results and answers with the finished run. It must
finish within Vercel's time limit (vercel.json maxDuration, 300 s), so the command gets a deadline: each step starts new
work (a business, a search, an address, an email) only while there is time for it, and then says what is left;
starting the run again continues there, because finished work is skipped. The full pipeline runs its steps in order
until the deadline. Only steps that need a browser on this machine (the built-in Google Maps browser) cannot run here.
"""
import os
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Value
from django.db.models.functions import Concat
from django.utils import timezone

from pipeline import jobs
from pipeline.models import PipelineRun

TIME_LIMIT = 270  # seconds a run may take; Vercel ends the request at 300 (vercel.json maxDuration)
STALE_AFTER = timedelta(minutes=6)  # a web run still "running" after this was cut off by Vercel
BROWSER_ONLY = {"refresh_businesses": "Refreshing Google Maps details needs the built-in browser"}


def web_capable(command: str, arguments: list[str]) -> bool:
    """Every run executes in its request on Vercel."""
    return bool(settings.ON_VERCEL)


def refusal(command: str, arguments: list[str]) -> str | None:
    """Why this run cannot work on the web app, or None."""
    if command in BROWSER_ONLY:
        return f"{BROWSER_ONLY[command]}, which the web app does not have; run it from a machine with Chromium."
    if command == "verify_emails":
        from pipeline.services import verification

        url = os.environ.get("REACHER_URL", "").strip()
        if not url or verification.is_local(url) or not os.environ.get("REACHER_SECRET", "").strip():
            return ("Verifying emails on the web app needs your server's Reacher: set REACHER_URL "
                    "(https://your-domain/reacher) and REACHER_SECRET in Vercel.")
    return None


def execute(run_id: int, time_limit: float = TIME_LIMIT) -> None:
    """Run a web run here and now, in this request, and record the result."""
    if not PipelineRun.objects.filter(pk=run_id, runs_on="web", status=PipelineRun.Status.QUEUED).update(
            status=PipelineRun.Status.RUNNING, started_at=timezone.now()):
        return  # already run or cancelled
    run = PipelineRun.objects.get(pk=run_id)
    output = jobs.RunOutput(run_id)
    status, code = PipelineRun.Status.FAILED, 1
    with redirect_stdout(output), redirect_stderr(output):
        print(f"$ {run.command_line}")
        problem = refusal(run.command, run.arguments)
        if problem:
            print(f"Error: {problem}")
        else:
            print(f"Runs on the web app (up to {time_limit / 60:.1f} minutes; what is left continues when you run it "
                  "again).", flush=True)
            try:
                call_command(run.command, *run.arguments, deadline=time.monotonic() + time_limit)
                status, code = PipelineRun.Status.SUCCEEDED, 0
            except CommandError as error:
                print(f"Error: {error}")
                code = error.returncode or 1
            except Exception:  # noqa: BLE001 - the traceback is the useful output
                import traceback

                traceback.print_exc()
    PipelineRun.objects.filter(pk=run_id).update(status=status, exit_code=code, finished_at=timezone.now(),
                                                 output=output.text())


def fail_cut_off_runs() -> None:
    """Web runs Vercel ended mid-request (they never recorded a result) are marked failed."""
    PipelineRun.objects.filter(runs_on="web", status=PipelineRun.Status.RUNNING,
                               started_at__lt=timezone.now() - STALE_AFTER).update(
        status=PipelineRun.Status.FAILED, finished_at=timezone.now(),
        output=Concat("output", Value("\n[The request running this was ended before it finished; start the run "
                                      "again with a smaller --limit.]\n")))
