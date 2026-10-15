"""Runs the web app on Vercel executes itself, inside the request that starts them: no worker, no queue.

Only steps that are nothing but HTTP calls to your own server run this way: fetching leads through the scraper
container (MAPS_SCRAPER_URL + MAPS_SCRAPER_KEY) and verifying emails with Reacher (REACHER_URL + REACHER_SECRET),
when both point at a remote server. The request calls the container, waits for its answer, saves the results and
answers, all within Vercel's time limit (vercel.json maxDuration, 300 s): a run does what fits (one or two Google Maps
searches, or the addresses it can check in a few minutes) and says what is left; starting it again continues there.
Everything else (browsers, Docker) still goes to the worker.
"""
import os
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

from django.conf import settings
from django.core.management.base import CommandError
from django.db.models import Value
from django.db.models.functions import Concat
from django.utils import timezone

from pipeline import jobs
from pipeline.models import PipelineRun

TIME_LIMIT = 270  # seconds a run may take; Vercel ends the request at 300 (vercel.json maxDuration)
SEARCH_PLACES = 20  # places one Google Maps search asks for, so a search fits in the time limit
STALE_AFTER = timedelta(minutes=6)  # a web run still "running" after this was cut off by Vercel


def web_capable(command: str, arguments: list[str]) -> bool:
    """Whether the web app runs this itself: on Vercel (queue mode), for steps that only call your own server."""
    if not (settings.ON_VERCEL and jobs.queue_mode()):
        return False
    try:
        options = vars(jobs.command_parser(command).parse_args([str(a) for a in arguments]))
    except (ValueError, CommandError):
        return False
    if command == "verify_emails":
        from pipeline.services import verification

        url = os.environ.get("REACHER_URL", "").strip()
        return bool(url and not verification.is_local(url) and os.environ.get("REACHER_SECRET", "").strip())
    if command == "fetch_businesses":
        from pipeline.models import DiscoveryCampaign
        from pipeline.services import scraper_api

        source = options.get("source") or DiscoveryCampaign.objects.filter(
            pk=options.get("campaign_id")).values_list("lead_source", flat=True).first()
        return (source == "scraper_api" and not scraper_api.is_local()
                and bool(os.environ.get("MAPS_SCRAPER_KEY", "").strip()))
    return False


def execute(run_id: int, time_limit: float = TIME_LIMIT) -> None:
    """Run a web run here and now, in this request, and record the result."""
    if not PipelineRun.objects.filter(pk=run_id, runs_on="web", status=PipelineRun.Status.QUEUED).update(
            status=PipelineRun.Status.RUNNING, started_at=timezone.now()):
        return  # already run or cancelled
    run = PipelineRun.objects.get(pk=run_id)
    started = time.monotonic()
    output = jobs.RunOutput(run_id)
    status, code = PipelineRun.Status.FAILED, 1
    with redirect_stdout(output), redirect_stderr(output):
        print(f"$ {run.command_line}")
        print(f"Runs on the web app, calling your server directly (up to {time_limit / 60:.1f} minutes).", flush=True)
        try:
            options = vars(jobs.command_parser(run.command).parse_args([str(a) for a in run.arguments]))
            code = STEPS[run.command](options, started + time_limit)
            status = PipelineRun.Status.SUCCEEDED if code == 0 else PipelineRun.Status.FAILED
        except CommandError as error:
            print(f"Error: {error}")
        except Exception:  # noqa: BLE001 - the traceback is the useful output
            traceback.print_exc()
    PipelineRun.objects.filter(pk=run_id).update(status=status, exit_code=code, finished_at=timezone.now(),
                                                 output=output.text())


def verify(options: dict, end: float) -> int:
    from pipeline.services import verification

    timeout = options["reacher_timeout"]
    return verification.run({**options, "no_auto_start": True, "skip_unknown": True,
                             "deadline": end - timeout})  # the last check may take the whole Reacher timeout


def fetch(options: dict, end: float) -> int:
    from pipeline.services import maps

    return maps.run({**options, "deadline": end, "max_per_search": SEARCH_PLACES})


STEPS = {"verify_emails": verify, "fetch_businesses": fetch}


def fail_cut_off_runs() -> None:
    """Web runs Vercel ended mid-request (they never recorded a result) are marked failed."""
    PipelineRun.objects.filter(runs_on="web", status=PipelineRun.Status.RUNNING,
                               started_at__lt=timezone.now() - STALE_AFTER).update(
        status=PipelineRun.Status.FAILED, finished_at=timezone.now(),
        output=Concat("output", Value("\n[The request running this was ended before it finished; start the run "
                                      "again with a smaller --limit.]\n")))
