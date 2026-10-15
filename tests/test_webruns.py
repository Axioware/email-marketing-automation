"""Runs the web app on Vercel executes itself, inside the request that starts them (pipeline/webruns.py)."""
import json
import os
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pipeline import jobs, webruns
from pipeline.models import Business, DiscoveryCampaign, PipelineRun, Prospect
from tests.fakes import FakeReacher
from tests.support import make_business, make_campaign, make_contact, run_command
from tests.test_scraper_api import PLACES

VERCEL = {"RUN_MODE": "queue"}
REMOTE_REACHER = {"REACHER_URL": "https://vps.example.com/reacher", "REACHER_SECRET": "s3cret"}
REMOTE_SCRAPER = {"MAPS_SCRAPER_URL": "https://vps.example.com/scraper", "MAPS_SCRAPER_KEY": "k3y"}
TOKEN = "t" * 20


class FakeScraper:
    """The scraper container's /scrape-get: answers after `seconds`, checking the key."""

    def __init__(self, places=PLACES, seconds=0.0, key="k3y"):
        self.places, self.seconds, self.key, self.requests = places, seconds, key, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                if self.headers.get("X-API-Key") != fake.key:
                    code, body = 401, {"detail": "Wrong or missing X-API-Key header."}
                elif url.path == "/scrape-get":
                    fake.requests.append({k: v[0] for k, v in parse_qs(url.query).items()})
                    time.sleep(fake.seconds)
                    code, body = 200, fake.places[:int(fake.requests[-1]["max_places"])]
                else:
                    code, body = 200, {"message": "running"}
                data = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the caller gave up waiting

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@override_settings(ON_VERCEL=True)
class RoutingTests(TestCase):
    def start(self, command, arguments, env):
        with mock.patch.dict(os.environ, env), mock.patch.object(webruns, "execute") as execute, \
                mock.patch.object(jobs, "launch") as launch, self.captureOnCommitCallbacks(execute=True):
            run = jobs.start_run(command, arguments)
        run.refresh_from_db()
        return run, execute, launch

    def test_verification_against_your_server_runs_in_the_request(self):
        run, execute, launch = self.start("verify_emails", [], {**VERCEL, **REMOTE_REACHER})
        self.assertEqual(run.runs_on, "web")
        execute.assert_called_once_with(run.pk)
        launch.assert_not_called()

    def test_scraper_fetch_against_your_server_runs_in_the_request(self):
        campaign = make_campaign(lead_source="scraper_api")
        run, execute, _ = self.start("fetch_businesses", ["--campaign-id", campaign.pk, "--limit", "5"],
                                     {**VERCEL, **REMOTE_SCRAPER})
        self.assertEqual(run.runs_on, "web")
        execute.assert_called_once()
        run, execute, _ = self.start("fetch_businesses", ["--campaign-id", campaign.pk, "--limit", "5", "--source",
                                                          "browser"], {**VERCEL, **REMOTE_SCRAPER})
        self.assertEqual(run.runs_on, "worker")  # the built-in browser needs the worker
        execute.assert_not_called()

    def test_everything_else_goes_to_the_worker(self):
        campaign = make_campaign(lead_source="scraper_api")
        cases = [
            ("verify_emails", [], {**VERCEL, "REACHER_URL": "http://127.0.0.1:8080", "REACHER_SECRET": "s"}),
            ("verify_emails", [], {**VERCEL, "REACHER_URL": REMOTE_REACHER["REACHER_URL"], "REACHER_SECRET": ""}),
            ("fetch_businesses", ["--campaign-id", campaign.pk, "--limit", "5"],
             {**VERCEL, "MAPS_SCRAPER_URL": "http://127.0.0.1:8001", "MAPS_SCRAPER_KEY": "k"}),
            ("fetch_businesses", ["--campaign-id", campaign.pk, "--limit", "5"],
             {**VERCEL, "MAPS_SCRAPER_URL": REMOTE_SCRAPER["MAPS_SCRAPER_URL"], "MAPS_SCRAPER_KEY": ""}),
            ("research_websites", [], {**VERCEL, **REMOTE_REACHER, **REMOTE_SCRAPER}),
        ]
        for command, arguments, env in cases:
            run, execute, _ = self.start(command, arguments, env)
            self.assertEqual(run.runs_on, "worker", (command, env))
            execute.assert_not_called()

    @override_settings(ON_VERCEL=False)
    def test_off_vercel_nothing_runs_in_the_request(self):
        run, execute, _ = self.start("verify_emails", [], {**VERCEL, **REMOTE_REACHER})
        self.assertEqual(run.runs_on, "worker")
        execute.assert_not_called()


@override_settings(ON_VERCEL=True, AUTH_TOKEN=TOKEN)
class DirectRunTests(TransactionTestCase):
    """The request that starts the run calls the container and answers with the finished run (committed for real,
    as on Vercel, so the run starts when its row is saved)."""

    def setUp(self):
        self.scraper = FakeScraper()
        self.addCleanup(self.scraper.close)
        self.reacher = FakeReacher()
        self.reacher.secret = "s3cret"
        self.addCleanup(self.reacher.close)
        self.campaign = make_campaign(target_country="Pakistan", search_terms=["dental clinic", "dentist"],
                                      target_locations=["Lahore"], lead_source="scraper_api")
        env = {**VERCEL, "MAPS_SCRAPER_URL": self.scraper.url, "MAPS_SCRAPER_KEY": "k3y",
               "REACHER_URL": self.reacher.url, "REACHER_SECRET": "s3cret", "VERIFY_MAIL_FROM": "", "VERIFY_HELO": ""}
        patchers = [mock.patch.dict(os.environ, env),
                    mock.patch.object(webruns, "web_capable", return_value=True)]  # fakes run on 127.0.0.1
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def post_run(self, command, options):
        return self.client.post("/api/runs/", {"command": command, "options": options},
                                content_type="application/json", HTTP_AUTH=TOKEN)

    def test_fetch_answers_with_the_saved_businesses(self):
        response = self.post_run("fetch_businesses", {"campaign_id": self.campaign.pk, "limit": 2})
        run = response.json()
        self.assertEqual((run["status"], run["runs_on"]), ("succeeded", "web"), run["output"])
        self.assertIn("calling your server directly", run["output"])
        self.assertIn("Stored 2 businesses", run["output"])
        self.assertEqual(Business.objects.filter(discovery_campaign=self.campaign).count(), 2)
        self.assertEqual(self.scraper.requests[0]["max_places"], "2")
        self.assertEqual(DiscoveryCampaign.objects.get(pk=self.campaign.pk).status, "completed")

    def test_a_search_asks_for_at_most_search_places(self):
        self.post_run("fetch_businesses", {"campaign_id": self.campaign.pk, "limit": 100})
        self.assertEqual(int(self.scraper.requests[0]["max_places"]), webruns.SEARCH_PLACES)

    def test_fetch_stops_at_the_time_limit(self):
        self.scraper.seconds = 0.3
        run = PipelineRun.objects.create(command="fetch_businesses", runs_on="web",
                                         arguments=["--campaign-id", str(self.campaign.pk), "--limit", "50"])
        with mock.patch("pipeline.services.scraper_api.MIN_SEARCH_SECONDS", 0.5):
            webruns.execute(run.pk, time_limit=0.6)  # time for one search only
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded", run.output)
        self.assertEqual(len(self.scraper.requests), 1)
        self.assertIn("Time limit reached: 1 search(es) not run; start the run again", run.output)

    def test_a_search_slower_than_the_time_left_is_skipped(self):
        self.scraper.seconds = 2
        run = PipelineRun.objects.create(command="fetch_businesses", runs_on="web",
                                         arguments=["--campaign-id", str(self.campaign.pk), "--limit", "2"])
        with mock.patch("pipeline.services.scraper_api.MIN_SEARCH_SECONDS", 0.2):
            webruns.execute(run.pk, time_limit=0.5)
        run.refresh_from_db()
        self.assertIn("took longer than", run.output)
        self.assertEqual(Business.objects.count(), 0)

    def test_wrong_scraper_key_fails_the_run(self):
        self.scraper.key = "other"
        run = self.post_run("fetch_businesses", {"campaign_id": self.campaign.pk, "limit": 2}).json()
        self.assertEqual(run["status"], "failed")
        self.assertIn("refused the key", run["output"])

    def test_verify_answers_with_the_results(self):
        campaign = make_campaign()
        make_contact(make_business(campaign, "A"), "good@a.org", ["grey@a.org"])
        make_contact(make_business(campaign, "B"), "bad@b.org")
        run = self.post_run("verify_emails", {"delay": 0}).json()
        self.assertEqual(run["status"], "succeeded", run["output"])
        self.assertEqual(sorted(self.reacher.calls), ["bad@b.org", "good@a.org", "grey@a.org"])
        self.assertEqual(Prospect.objects.get(email="good@a.org").email_status, "deliverable")
        again = self.post_run("verify_emails", {"delay": 0}).json()
        self.assertEqual(again["status"], "succeeded", again["output"])
        self.assertEqual(len(self.reacher.calls), 3)  # unknown (grey) is not checked again; --recheck does that

    def test_verify_stops_at_the_time_limit_and_the_next_run_continues(self):
        campaign = make_campaign()
        for index, email in enumerate(["good@a.org", "bad@b.org", "good2@c.org"]):
            make_contact(make_business(campaign, f"B{index}"), email)
        self.reacher.delays = {"good": 0.3, "bad": 0.3, "good2": 0.3}
        run = PipelineRun.objects.create(command="verify_emails", runs_on="web",
                                         arguments=["--delay", "0", "--reacher-timeout", "0.1"])
        webruns.execute(run.pk, time_limit=0.15)  # new checks start only in the first 0.05 s
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded", run.output)
        self.assertEqual(len(self.reacher.calls), 1)
        self.assertIn("Time limit reached: 2 contact(s) not finished; start the run again", run.output)
        rest = PipelineRun.objects.create(command="verify_emails", runs_on="web", arguments=["--delay", "0"])
        webruns.execute(rest.pk)
        self.assertEqual(sorted(self.reacher.calls), ["bad@b.org", "good2@c.org", "good@a.org"])

    def test_wrong_reacher_key_fails_the_run(self):
        self.reacher.secret = "other"
        make_contact(make_business(make_campaign(), "A"), "good@a.org")
        run = self.post_run("verify_emails", {"delay": 0}).json()
        self.assertEqual(run["status"], "failed")
        self.assertIn("refused the key", run["output"])
        self.assertEqual(self.reacher.calls, [])

    def test_admin_form_runs_it_before_showing_the_run(self):
        self.client.force_login(get_user_model().objects.create_superuser("runs", "r@example.org", "pw"))
        with mock.patch.object(jobs, "launch") as launch:
            response = self.client.post(reverse("admin:pipeline_pipelinerun_add"), {
                "command": "fetch_businesses", "arguments_text": f"--campaign-id {self.campaign.pk} --limit 1"})
        run = PipelineRun.objects.get()
        self.assertRedirects(response, reverse("admin:pipeline_pipelinerun_change", args=[run.pk]),
                             fetch_redirect_response=False)
        self.assertEqual((run.runs_on, run.status), ("web", "succeeded"), run.output)
        launch.assert_not_called()  # never a process on Vercel

    def test_a_run_executes_once(self):
        run = PipelineRun.objects.create(command="verify_emails", runs_on="web", arguments=["--delay", "0"])
        webruns.execute(run.pk)
        output = PipelineRun.objects.get(pk=run.pk).output
        webruns.execute(run.pk)
        self.assertEqual(PipelineRun.objects.get(pk=run.pk).output, output)


class CutOffTests(TestCase):
    def test_a_run_vercel_cut_off_is_marked_failed(self):
        old = PipelineRun.objects.create(command="verify_emails", runs_on="web", status="running",
                                         started_at=timezone.now() - timedelta(minutes=10))
        fresh = PipelineRun.objects.create(command="verify_emails", runs_on="web", status="running",
                                           started_at=timezone.now())
        jobs.refresh_stale_runs()
        self.assertEqual(PipelineRun.objects.get(pk=old.pk).status, "failed")
        self.assertIn("ended before it finished", PipelineRun.objects.get(pk=old.pk).output)
        self.assertEqual(PipelineRun.objects.get(pk=fresh.pk).status, "running")
        self.assertIn("finishes on its own", jobs.cancel_run(PipelineRun.objects.get(pk=fresh.pk)))

    def test_worker_leaves_web_runs_alone(self):
        run = PipelineRun.objects.create(command="verify_emails", runs_on="web")
        PipelineRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(hours=1))
        jobs.fail_dead_runs(queued_too=True)
        self.assertEqual(PipelineRun.objects.get(pk=run.pk).status, "queued")
        with mock.patch("pipeline.management.commands.run_worker.heartbeat"), \
                mock.patch("pipeline.management.commands.run_worker.close_old_connections"), \
                mock.patch("pipeline.management.commands.run_worker.Command.run_one") as run_one:
            run_command("run_worker", "--once")
        run_one.assert_not_called()
        self.assertEqual(jobs.execute_run(run.pk), 0)
        self.assertEqual(PipelineRun.objects.get(pk=run.pk).status, "queued")

    def test_vercel_allows_the_time_limit(self):
        from pathlib import Path

        from django.conf import settings

        config = json.loads((Path(settings.BASE_DIR) / "vercel.json").read_text())
        self.assertGreater(config["functions"]["emailautomation/wsgi.py"]["maxDuration"], webruns.TIME_LIMIT)
