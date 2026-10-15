"""Runs the web app on Vercel executes itself, in short batches, instead of waiting for a worker.

Only steps that are nothing but HTTP calls to your own server run this way: fetching leads through the scraper
container (MAPS_SCRAPER_URL + MAPS_SCRAPER_KEY) and verifying emails with Reacher (REACHER_URL + REACHER_SECRET), when
both point at a remote server. Everything else (browsers, Docker) still goes to the worker.

Each batch runs for at most BATCH_SECONDS inside one function invocation; the run's `progress` keeps where it is and
the next batch continues there. On Vercel, a Vercel Queues task (pipeline/tasks.py) runs a batch and queues the next.
A batch holds a lease on the run, so two batches never work on it at once; a batch that died (timeout, crash) lets its
lease expire, and the queue delivers it again or the run page / POST /api/runs/{id}/continue/ continues it.
"""
import os
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import CommandError
from django.db.models import F, Q, Value
from django.db.models.functions import Coalesce, Concat
from django.utils import timezone

from pipeline import jobs
from pipeline.models import PipelineRun

BATCH_SECONDS = 180  # new work starts only within this; a Reacher check (90 s) may run past it, within Vercel's 300 s
LEASE = timedelta(seconds=330)  # longer than any batch can run
STALLED_AFTER = timedelta(minutes=6)  # no batch finished for this long (and none is working): continue it
POLL_SECONDS = 5  # between looks at a scraper search


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


def kick(run_id: int) -> str | None:
    """Queue the run's next batch (Vercel Queues). Returns what went wrong, or None."""
    try:
        from pipeline import tasks

        tasks.advance_run.delay(run_id)
    except Exception as error:  # noqa: BLE001 - reported on the run; Continue retries
        message = f"Could not queue the next batch ({type(error).__name__}: {error})."[:500]
        PipelineRun.objects.filter(pk=run_id).update(output=Concat(F("output"), Value(f"\n[{message}]\n")))
        return message
    return None


def stalled(run: PipelineRun) -> bool:
    """A web run that is not finished, has no batch working on it, and has not moved for STALLED_AFTER."""
    if run.runs_on != "web" or run.is_finished:
        return False
    now = timezone.now()
    if run.lease_until and run.lease_until > now:
        return False
    last = (run.progress or {}).get("batch_at")
    since = datetime.fromisoformat(last) if last else run.created_at
    return now - since > STALLED_AFTER


def continue_run(run: PipelineRun) -> str:
    if run.runs_on != "web":
        return "Only runs the web app executes in batches can be continued."
    if run.is_finished:
        return "This run has finished."
    if run.lease_until and run.lease_until > timezone.now():
        return "A batch is working on this run right now."
    return kick(run.pk) or "The next batch is queued."


def cancel(run: PipelineRun) -> str:
    """Stop a web run: at once when no batch is working on it, else after the current batch."""
    PipelineRun.objects.filter(pk=run.pk).update(cancel_requested=True)
    if PipelineRun.objects.filter(pk=run.pk, status__in=[PipelineRun.Status.QUEUED, PipelineRun.Status.RUNNING]).filter(
            Q(lease_until__isnull=True) | Q(lease_until__lt=timezone.now())).update(
            status=PipelineRun.Status.CANCELLED, finished_at=timezone.now(), output=Concat(F("output"), Value("\nCancelled.\n"))):
        return "Cancelled."
    return f"Stop requested; the run stops after its current batch (at most {BATCH_SECONDS // 60 + 2} minutes)."


def claim(run_id: int) -> bool:
    now = timezone.now()
    return PipelineRun.objects.filter(
        pk=run_id, runs_on="web", status__in=[PipelineRun.Status.QUEUED, PipelineRun.Status.RUNNING],
    ).filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now)).update(
        status=PipelineRun.Status.RUNNING, lease_until=now + LEASE, started_at=Coalesce(F("started_at"), now),
    ) == 1


def advance(run_id: int, batch_seconds: float = BATCH_SECONDS) -> bool:
    """Run the run's next batch. Returns whether another batch is needed (False also when another batch holds the
    run, or it is finished)."""
    if not claim(run_id):
        return False
    run = PipelineRun.objects.get(pk=run_id)
    if run.cancel_requested:
        _finish(run, PipelineRun.Status.CANCELLED, -1, run.output + "\nCancelled.\n", run.progress)
        return False
    deadline = time.monotonic() + batch_seconds
    progress = dict(run.progress or {})
    output = jobs.RunOutput(run_id)
    output.parts, output.size = [run.output], len(run.output)
    code, more = 1, False
    with redirect_stdout(output), redirect_stderr(output):
        if not progress:
            print(f"$ {run.command_line}")
            size = f"{batch_seconds / 60:.0f} minutes" if batch_seconds >= 90 else f"{batch_seconds:.0f} seconds"
            print(f"Runs on the web app in batches of about {size}.", flush=True)
        try:
            options = vars(jobs.command_parser(run.command).parse_args([str(a) for a in run.arguments]))
            code, more = STEPS[run.command](options, progress, deadline)
        except CommandError as error:
            print(f"Error: {error}")
        except Exception:  # noqa: BLE001 - the traceback is the useful output
            traceback.print_exc()
            more = False
    progress["batch_at"] = timezone.now().isoformat()
    if more:
        PipelineRun.objects.filter(pk=run_id).update(output=output.text(), progress=progress, lease_until=None)
        return True
    status = PipelineRun.Status.SUCCEEDED if code == 0 else PipelineRun.Status.FAILED
    _finish(run, status, code, output.text(), progress)
    return False


def _finish(run: PipelineRun, status: str, code: int, text: str, progress: dict) -> None:
    PipelineRun.objects.filter(pk=run.pk).update(status=status, exit_code=code, finished_at=timezone.now(),
                                                 output=text, progress=progress, lease_until=None)


# ---------------------------------------------------------------- the steps


def verify_batch(options: dict, progress: dict, deadline: float) -> tuple[int, bool]:
    from pipeline.services import verification

    code = verification.run({**options, "progress": progress, "deadline": deadline, "no_auto_start": True,
                             "skip_sender_check": progress.get("sender_checked", False)})
    progress["sender_checked"] = True
    more = bool(progress.pop("more", False)) and code == 0
    if more:
        print("Continuing in the next batch.", flush=True)
    return code, more


def fetch_batch(options: dict, progress: dict, deadline: float) -> tuple[int, bool]:
    """Fetch through the scraper container's background searches: start a search, look at it every few seconds,
    save what it found, go on with the next query, until the limit or the queries are used up."""
    from pipeline.services import maps, scraper_api

    if "queries" not in progress:
        campaign = _start_fetch(options, progress)
        if campaign is None:
            return 1, False
    campaign = maps.choose_campaign(progress["campaign_id"])
    if campaign is None:
        return 1, False
    queries, limit = progress["queries"], progress["limit"]
    try:
        while progress["saved"] < limit and progress["index"] < len(queries):
            if time.monotonic() >= deadline:
                print("Continuing in the next batch.", flush=True)
                return 0, True
            if not progress.get("job"):
                seen = maps.existing_businesses(campaign["id"])
                known = sum(1 for business in seen if business.get("discovery_campaign_id") == campaign["id"])
                wanted = min(limit - progress["saved"] + known, scraper_api.MAX_PLACES_PER_SEARCH)
                query = queries[progress["index"]]
                print(f"Searching Google Maps through the scraper container: {query} (up to {wanted} places)",
                      flush=True)
                progress["job"] = scraper_api.start_job(query, wanted, options.get("lang") or "en",
                                                        options.get("concurrency") or 5)
                continue
            state = scraper_api.job(progress["job"])
            if state is None:
                print("  The scraper container no longer knows this search (it restarted); searching again.")
                progress["job"] = None
                continue
            if state["status"] in ("queued", "running"):
                time.sleep(max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))
                continue
            if state["status"] == "failed":
                print(f"  Skipped this search: {state.get('error') or 'it failed'}.")
            else:
                seen, records = maps.existing_businesses(campaign["id"]), []
                for place in state["results"]:
                    if len(records) >= limit - progress["saved"]:
                        break
                    record = scraper_api.record_from_place(place, campaign["target_country"])
                    if record is not None:
                        maps.accept_record(record, campaign, seen, records)
                saved = maps.save_records(records) if records else 0
                progress["saved"] += saved
                print(f"  {len(state['results'])} place(s) found, {saved} new.", flush=True)
            progress["job"], progress["index"] = None, progress["index"] + 1
    except scraper_api.ScraperUnavailable as error:
        print(f"Campaign {campaign['id']} failed: {error}.")
        maps.update_campaign_status(campaign["id"], "failed")
        return 1, False
    maps.update_campaign_status(campaign["id"], "completed")
    if progress["saved"]:
        print(f"Stored {progress['saved']} businesses for campaign {campaign['id']}.")
    else:
        print("No new businesses were found for this campaign (businesses any campaign already has are skipped).")
    return 0, False


def _start_fetch(options: dict, progress: dict):
    """First batch of a fetch: the same checks as `fetch_businesses`, then remember the queries."""
    from pipeline.services import maps, scraper_api

    if options["campaign_id"] is None or options["limit"] is None:
        print("Pass --campaign-id and --limit.")
        return None
    if options["limit"] < 1 or not 1 <= (options.get("concurrency") or 5) <= 20:
        print("--limit must be a positive integer and --concurrency between 1 and 20.")
        return None
    campaign = maps.choose_campaign(options["campaign_id"])
    if campaign is None:
        return None
    queries = maps.build_search_queries(campaign)
    if not queries:
        return None
    problem = scraper_api.status()
    if problem:
        print(f"The scraper container at {scraper_api.base_url()} is {problem}.")
        return None
    print("Automated Google Maps access may be restricted by Google's terms. "
          "For production use, prefer the official Places API.")
    print("Lead source: scraper container (on your server).")
    progress.update(campaign_id=campaign["id"], queries=queries, limit=options["limit"], index=0, saved=0, job=None)
    maps.update_campaign_status(campaign["id"], "running")
    return campaign


STEPS = {"verify_emails": verify_batch, "fetch_businesses": fetch_batch}
