"""Pipeline runs: argument building, in-process execution, stale-run cleanup, and a real background process."""
import os
import time
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from pipeline import jobs
from pipeline.models import PipelineRun
from tests.support import make_email


class ArgumentTests(TestCase):
    def test_options_become_arguments(self):
        self.assertEqual(
            jobs.build_arguments("verify_emails", {"limit": 3, "dry_run": True, "probe_all": False, "business_id": [1, 2],
                                                   "min-score": 40, "helo": None}),
            ["--limit", "3", "--dry-run", "--business-id", "1", "--business-id", "2", "--min-score", "40"],
        )
        self.assertEqual(jobs.build_arguments("generate_emails", {"prospect_id": 7}), ["--prospect-id", "7"])
        self.assertEqual(jobs.build_arguments("send_emails", arguments="--limit 2 --send"), ["--limit", "2", "--send"])

    def test_invalid_input_is_reported(self):
        for command, options, arguments, message in (
            ("migrate", None, None, "Unknown command"),
            ("verify_emails", {"nope": 1}, None, "Unknown option"),
            ("verify_emails", {"limit": "abc"}, None, "invalid int value"),
            ("verify_emails", {"dry_run": "yes"}, None, "is a flag"),
            ("verify_emails", {"limit": [1, 2]}, None, "single value"),
            ("verify_emails", None, "--limit", "expected one argument"),
            ("verify_emails", None, "'unclosed", "Cannot parse"),
            ("verify_emails", {"report": "/etc/cron.d/x"}, None, "Unknown option"),
            ("verify_emails", None, "--report /home/x/.bashrc", "cannot be set from the admin"),
            ("verify_emails", None, "--rep /tmp/x.csv", "cannot be set from the admin"),  # abbreviations too
            ("find_stakeholders", None, "--roles-file /etc/passwd", "cannot be set from the admin"),
            ("find_stakeholders", None, "--profile /home/x/.mozilla", "cannot be set from the admin"),
            ("verify_emails", None, "--reacher-url http://127.0.0.1:9", "cannot be set from the admin"),
            ("verify_emails", None, "--settings evil.module", "cannot be set from the admin"),
            ("verify_emails", None, "--pythonpath /tmp", "cannot be set from the admin"),
        ):
            with self.assertRaises(ValueError, msg=(command, options, arguments)) as caught:
                jobs.build_arguments(command, options, arguments)
            self.assertIn(message, str(caught.exception))

    def test_describe_command(self):
        described = jobs.describe_command("find_stakeholders")
        flags = {o["flag"] for o in described["options"]}
        self.assertIn("--max-searches", flags)
        self.assertNotIn("--verbosity", flags)
        self.assertEqual(next(o for o in described["options"] if o["name"] == "min_score")["default"], 50)


class ExecuteTests(TestCase):
    def test_success_records_output_and_status(self):
        run = PipelineRun.objects.create(command="send_emails", arguments=["--delay", "0"])
        self.assertEqual(jobs.execute_run(run.pk), 0)
        run.refresh_from_db()
        self.assertEqual((run.status, run.exit_code), ("succeeded", 0))
        self.assertIn("$ python manage.py send_emails --delay 0", run.output)
        self.assertIn("No approved emails ready to send.", run.output)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.pid, os.getpid())

    def test_command_error_is_recorded_as_failed(self):
        run = PipelineRun.objects.create(command="send_emails", arguments=["--delay", "-1"])
        self.assertEqual(jobs.execute_run(run.pk), 1)
        run.refresh_from_db()
        self.assertEqual((run.status, run.exit_code), ("failed", 1))
        self.assertIn("Error: --delay cannot be negative", run.output)

    def test_unexpected_exception_keeps_the_traceback(self):
        run = PipelineRun.objects.create(command="send_emails", arguments=[])
        with mock.patch("pipeline.services.sending.run", side_effect=RuntimeError("boom")):
            jobs.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertIn("RuntimeError: boom", run.output)
        self.assertIn("Traceback", run.output)

    def test_a_run_executes_once(self):
        run = PipelineRun.objects.create(command="send_emails", arguments=["--delay", "0"])
        jobs.execute_run(run.pk)
        output = PipelineRun.objects.get().output
        jobs.execute_run(run.pk)  # already finished: nothing happens
        self.assertEqual(PipelineRun.objects.get().output, output)

    def test_cancelled_before_start_never_runs(self):
        run = PipelineRun.objects.create(command="send_emails")
        self.assertIn("before it started", jobs.cancel_run(run))
        jobs.execute_run(run.pk)
        self.assertEqual(PipelineRun.objects.get().status, "cancelled")

    def test_dead_process_and_never_started_runs_are_marked_failed(self):
        dead = PipelineRun.objects.create(command="send_emails", status="running", pid=2 ** 22 + 12345, output="so far\n")
        stuck = PipelineRun.objects.create(command="send_emails")
        PipelineRun.objects.filter(pk=stuck.pk).update(created_at=timezone.now() - jobs.QUEUED_TIMEOUT * 2)
        fresh = PipelineRun.objects.create(command="send_emails")
        jobs.refresh_stale_runs()
        dead.refresh_from_db()
        self.assertEqual(dead.status, "failed")
        self.assertIn("so far", dead.output)
        self.assertIn("stopped without recording", dead.output)
        self.assertEqual(PipelineRun.objects.get(pk=stuck.pk).status, "failed")
        self.assertEqual(PipelineRun.objects.get(pk=fresh.pk).status, "queued")

    def test_output_is_trimmed_to_the_newest(self):
        output = jobs.RunOutput(0)
        output.last_save = time.monotonic() + 3600  # no saving during the test
        output.write("x" * jobs.MAX_OUTPUT_CHARS + "tail")
        self.assertTrue(output.text().endswith("tail"))
        self.assertTrue(output.text().startswith("[earlier output trimmed]"))


class BackgroundProcessTests(TransactionTestCase):
    """A real `manage.py run_pipeline_job` process against the test database."""

    def test_launch_runs_in_the_background_and_streams_output(self):
        make_email("approved")
        db = connection.settings_dict
        url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        run = PipelineRun.objects.create(command="send_emails", arguments=["--delay", "0"])
        with mock.patch.dict(os.environ, {"DATABASE_URL": url, "DJANGO_DEBUG": "1"}):
            jobs.launch(run.pk)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run.refresh_from_db()
            if run.is_finished:
                break
            time.sleep(0.5)
        self.assertEqual(run.status, "succeeded", run.output + jobs._log_tail(run.pk))
        self.assertIn("Preview (nothing is sent", run.output)
        self.assertIn("1 would be sent", run.output)
        self.assertNotEqual(run.pid, os.getpid())
