"""Runs the web app on Vercel executes itself in batches (pipeline/webruns.py): routing, batches, leases, recovery."""
import json
import os
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pipeline import jobs, tasks, webruns
from pipeline.models import Business, DiscoveryCampaign, PipelineRun, Prospect
from tests.fakes import FakeReacher
from tests.support import make_business, make_campaign, make_contact, run_command
from tests.test_scraper_api import PLACES

try:
    import tomllib
except ImportError:  # Python 3.10
    from pip._vendor import tomli as tomllib

VERCEL = {"RUN_MODE": "queue"}
REMOTE_REACHER = {"REACHER_URL": "https://vps.example.com/reacher", "REACHER_SECRET": "s3cret"}
REMOTE_SCRAPER = {"MAPS_SCRAPER_URL": "https://vps.example.com/scraper", "MAPS_SCRAPER_KEY": "k3y"}


class FakeJobs:
    """The scraper container's background searches: POST /jobs, GET /jobs/{id} (running `polls` times, then done)."""

    def __init__(self, places=PLACES, polls=2):
        self.places, self.polls, self.started, self.looks, self.forget = places, polls, [], {}, False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                url = urlsplit(self.path)
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                fake.started.append(query)
                self._send(202, {"id": f"job{len(fake.started)}", "status": "queued"})

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/":
                    return self._send(200, {"message": "running"})
                job_id = path.rsplit("/", 1)[-1]
                if fake.forget:
                    fake.forget = False
                    return self._send(404, {"detail": "Unknown job"})
                fake.looks[job_id] = fake.looks.get(job_id, 0) + 1
                if fake.looks[job_id] <= fake.polls:
                    return self._send(200, {"id": job_id, "status": "running", "error": None})
                self._send(200, {"id": job_id, "status": "done", "error": None, "results": fake.places})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def finish(run_id, batch_seconds=webruns.BATCH_SECONDS, limit=50):
    """Run batches until the run needs no more; returns how many ran."""
    for count in range(1, limit + 1):
        if not webruns.advance(run_id, batch_seconds):
            return count
    raise AssertionError("the run never finished")


@override_settings(ON_VERCEL=True)
class RoutingTests(TestCase):
    def start(self, command, arguments, env):
        with mock.patch.dict(os.environ, env), mock.patch.object(tasks.advance_run, "delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            run = jobs.start_run(command, arguments)
        run.refresh_from_db()
        return run, delay

    def test_verification_against_your_server_runs_on_the_web_app(self):
        run, delay = self.start("verify_emails", [], {**VERCEL, **REMOTE_REACHER})
        self.assertEqual((run.runs_on, run.status), ("web", "queued"))
        delay.assert_called_once_with(run.pk)

    def test_scraper_fetch_against_your_server_runs_on_the_web_app(self):
        campaign = make_campaign(lead_source="scraper_api")
        run, delay = self.start("fetch_businesses", ["--campaign-id", campaign.pk, "--limit", "5"],
                                {**VERCEL, **REMOTE_SCRAPER})
        self.assertEqual(run.runs_on, "web")
        delay.assert_called_once()
        run, _ = self.start("fetch_businesses", ["--campaign-id", campaign.pk, "--limit", "5", "--source", "browser"],
                            {**VERCEL, **REMOTE_SCRAPER})
        self.assertEqual(run.runs_on, "worker")  # the built-in browser needs the worker

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
            run, delay = self.start(command, arguments, env)
            self.assertEqual(run.runs_on, "worker", (command, env))
            delay.assert_not_called()

    @override_settings(ON_VERCEL=False)
    def test_off_vercel_nothing_runs_in_batches(self):
        run, delay = self.start("verify_emails", [], {**VERCEL, **REMOTE_REACHER})
        self.assertEqual(run.runs_on, "worker")
        delay.assert_not_called()

    def test_admin_form_routes_runs_the_same_way(self):
        user = get_user_model().objects.create_superuser("runs", "r@example.org", "pw")
        self.client.force_login(user)
        with mock.patch.dict(os.environ, {**VERCEL, **REMOTE_REACHER}), \
                mock.patch.object(tasks.advance_run, "delay") as delay, mock.patch.object(jobs, "launch") as launch, \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:pipeline_pipelinerun_add"), {"command": "verify_emails", "arguments_text": ""})
        run = PipelineRun.objects.get()
        self.assertEqual(run.runs_on, "web")
        delay.assert_called_once_with(run.pk)
        launch.assert_not_called()  # never a process on Vercel


class VerifyBatchTests(TestCase):
    def setUp(self):
        self.fake = FakeReacher()
        self.fake.secret = "s3cret"
        self.addCleanup(self.fake.close)
        campaign = make_campaign()
        self.contacts = [make_contact(make_business(campaign, f"B{i}"), email, candidates).pk for i, (email, candidates)
                         in enumerate([("good@a.org", ["grey@a.org"]), ("bad@b.org", []), ("good2@c.org", [])])]
        env = {"REACHER_URL": self.fake.url, "REACHER_SECRET": "s3cret", "VERIFY_MAIL_FROM": "", "VERIFY_HELO": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def web_run(self, arguments=("--delay", "0")):
        return PipelineRun.objects.create(command="verify_emails", arguments=list(arguments), runs_on="web")

    def test_batches_check_every_address_once(self):
        self.fake.delays = {"good": 0.3, "grey": 0.3, "bad": 0.3, "good2": 0.3}
        run = self.web_run()
        batches = finish(run.pk, batch_seconds=0.2)  # about one check per batch
        run.refresh_from_db()
        self.assertGreater(batches, 2)
        self.assertEqual(run.status, "succeeded", run.output)
        self.assertEqual(sorted(self.fake.calls), ["bad@b.org", "good2@c.org", "good@a.org", "grey@a.org"])
        self.assertIn("Continuing in the next batch.", run.output)
        self.assertEqual(Prospect.objects.filter(email_status="deliverable").count(), 2)
        self.assertEqual(Prospect.objects.get(email="grey@a.org").email_status, "unknown")  # not checked again
        self.assertIsNone(run.lease_until)

    def test_one_batch_when_there_is_time(self):
        run = self.web_run()
        self.assertEqual(finish(run.pk), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(len(self.fake.calls), 4)

    def test_wrong_key_fails_the_run(self):
        self.fake.secret = "other"
        run = self.web_run()
        self.assertEqual(finish(run.pk), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertIn("refused the key", run.output)
        self.assertEqual(self.fake.calls, [])


class FetchBatchTests(TestCase):
    def setUp(self):
        self.container = FakeJobs()
        self.addCleanup(self.container.close)
        self.campaign = make_campaign(target_country="Pakistan", search_terms=["dental clinic", "dentist"],
                                      target_locations=["Lahore"], lead_source="scraper_api")
        patchers = [mock.patch.dict(os.environ, {"MAPS_SCRAPER_URL": self.container.url, "MAPS_SCRAPER_KEY": "k3y"}),
                    mock.patch.object(webruns, "POLL_SECONDS", 0.05)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def web_run(self, limit=5):
        return PipelineRun.objects.create(command="fetch_businesses", runs_on="web",
                                          arguments=["--campaign-id", str(self.campaign.pk), "--limit", str(limit)])

    def test_searches_run_in_the_background_and_are_saved(self):
        run = self.web_run()
        finish(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded", run.output)
        self.assertEqual(Business.objects.filter(discovery_campaign=self.campaign).count(), 2)
        self.assertEqual([q["query"] for q in self.container.started],
                         ["dental clinic in Lahore, Pakistan", "dentist in Lahore, Pakistan"])
        self.assertIn("Stored 2 businesses", run.output)
        self.assertEqual(DiscoveryCampaign.objects.get(pk=self.campaign.pk).status, "completed")

    def test_a_long_search_spans_batches(self):
        self.container.polls = 4
        run = self.web_run()
        self.assertGreater(finish(run.pk, batch_seconds=0.12), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded", run.output)
        self.assertEqual(len(self.container.started), 2)  # each search started once, however many batches

    def test_limit_and_a_forgotten_job(self):
        self.container.forget = True
        run = self.web_run(limit=1)
        finish(run.pk)
        run.refresh_from_db()
        self.assertIn("searching again", run.output)
        self.assertEqual(Business.objects.filter(discovery_campaign=self.campaign).count(), 1)
        self.assertEqual(len(self.container.started), 2)  # the forgotten search, started again; then the limit

    def test_unreachable_container_fails_the_run(self):
        self.container.close()
        run = self.web_run()
        finish(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertIn("not reachable", run.output)


class LeaseAndRecoveryTests(TestCase):
    def run_row(self, **fields):
        return PipelineRun.objects.create(command="verify_emails", runs_on="web", **fields)

    def test_one_batch_at_a_time(self):
        run = self.run_row()
        self.assertTrue(webruns.claim(run.pk))
        self.assertFalse(webruns.claim(run.pk))
        PipelineRun.objects.filter(pk=run.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(webruns.claim(run.pk))  # a dead batch's lease expired

    def test_worker_runs_are_never_claimed(self):
        self.assertFalse(webruns.claim(PipelineRun.objects.create(command="verify_emails").pk))

    def test_stop(self):
        idle = self.run_row(status="running")
        self.assertEqual(jobs.cancel_run(idle), "Cancelled.")
        self.assertEqual(PipelineRun.objects.get(pk=idle.pk).status, "cancelled")
        busy = self.run_row(status="running", lease_until=timezone.now() + timedelta(minutes=5))
        self.assertIn("after its current batch", jobs.cancel_run(busy))
        PipelineRun.objects.filter(pk=busy.pk).update(lease_until=None)
        self.assertFalse(webruns.advance(busy.pk))
        self.assertEqual(PipelineRun.objects.get(pk=busy.pk).status, "cancelled")

    def test_stalled_run_is_continued(self):
        old = timezone.now() - timedelta(minutes=10)
        run = self.run_row(status="running", progress={"batch_at": old.isoformat()})
        self.assertTrue(webruns.stalled(run))
        self.assertFalse(webruns.stalled(self.run_row(status="running", progress={"batch_at": timezone.now().isoformat()})))
        self.assertFalse(webruns.stalled(self.run_row(status="running", lease_until=timezone.now() + timedelta(minutes=1),
                                                      progress={"batch_at": old.isoformat()})))
        user = get_user_model().objects.create_superuser("runs", "r@example.org", "pw")
        self.client.force_login(user)
        with mock.patch.object(tasks.advance_run, "delay") as delay:
            page = self.client.get(reverse("admin:pipeline_pipelinerun_change", args=[run.pk])).content.decode()
            delay.assert_called_once_with(run.pk)  # watching the run keeps it going
            self.assertIn("Continue now", page)
            self.client.post(reverse("admin:pipeline_pipelinerun_continue", args=[run.pk]))
            self.assertEqual(delay.call_count, 2)

    def test_queue_failure_is_reported_on_the_run(self):
        run = self.run_row()
        with mock.patch.object(tasks.advance_run, "delay", side_effect=RuntimeError("no queue")):
            self.assertIn("Could not queue", webruns.kick(run.pk))
        self.assertIn("no queue", PipelineRun.objects.get(pk=run.pk).output)

    def test_api_continue(self):
        token = "t" * 20
        run = self.run_row(status="running")
        with override_settings(AUTH_TOKEN=token), mock.patch.object(tasks.advance_run, "delay") as delay:
            response = self.client.post(f"/api/runs/{run.pk}/continue/", HTTP_AUTH=token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["run"]["runs_on"], "web")
        delay.assert_called_once_with(run.pk)

    def test_worker_leaves_web_runs_alone(self):
        run = self.run_row()
        PipelineRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(hours=1))
        jobs.fail_dead_runs(queued_too=True)
        self.assertEqual(PipelineRun.objects.get(pk=run.pk).status, "queued")
        with mock.patch("pipeline.management.commands.run_worker.heartbeat"), \
                mock.patch("pipeline.management.commands.run_worker.close_old_connections"), \
                mock.patch("pipeline.management.commands.run_worker.Command.run_one") as run_one:
            run_command("run_worker", "--once")
        run_one.assert_not_called()
        self.assertEqual(jobs.execute_run(run.pk), 0)  # never executed as a process either
        self.assertEqual(PipelineRun.objects.get(pk=run.pk).status, "queued")


class QueueTaskTests(TestCase):
    def test_a_batch_queues_the_next_until_done(self):
        with mock.patch.object(webruns, "advance", side_effect=[True, False]) as advance, \
                mock.patch.object(tasks, "connections") as connections, mock.patch.object(tasks, "close_old_connections"), \
                mock.patch.object(tasks.advance_run, "delay") as delay:
            tasks.advance_run(7)
            delay.assert_called_once_with(7)
            tasks.advance_run(7)
            delay.assert_called_once()
        self.assertEqual(advance.call_count, 2)
        self.assertEqual(connections.close_all.call_count, 2)  # nothing else closes them between deliveries

    def test_settings_for_vercel_queues(self):
        self.assertTrue(tasks.app.conf.task_acks_late)
        self.assertGreater(tasks.app.conf.broker_transport_options["lease_duration"], webruns.LEASE.total_seconds())

    def test_subscriber_entrypoint_imports(self):
        import vercel_worker

        self.assertIs(vercel_worker.app, tasks.app)


class PackagingTests(TestCase):
    def test_pyproject_matches_requirements(self):
        base = Path(settings.BASE_DIR)
        project = tomllib.loads((base / "pyproject.toml").read_text())
        wanted = [line.strip() for line in (base / "requirements.txt").read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        self.assertEqual(project["project"]["dependencies"], wanted)
        self.assertEqual(project["tool"]["vercel"]["entrypoint"], "emailautomation.wsgi:application")
        self.assertEqual(project["tool"]["vercel"]["subscribers"], [{"entrypoint": "vercel_worker:app"}])
