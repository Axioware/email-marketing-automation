"""The full pipeline command: step order, scoping to the campaign, and when it continues or stops."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from pipeline.models import Business, PipelineRun
from pipeline.services import generation, maps, research, stakeholders, verification
from tests.support import make_business, make_campaign, run_command

SERVICES = {"fetch": maps, "research": research, "stakeholders": stakeholders, "verify": verification,
            "generate": generation}


class PipelineCommandTests(TestCase):
    def setUp(self):
        self.calls = []
        self.results = {}
        self.campaign = make_campaign(name="Mine")
        self.other = make_business(make_campaign(name="Other"), "Elsewhere")
        for step, module in SERVICES.items():
            patcher = mock.patch.object(module, "run", side_effect=self.fake(step))
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake(self, step):
        def run(options):
            self.calls.append((step, options))
            if step == "fetch":  # fetching adds the campaign's businesses
                make_business(self.campaign, "Fetched")
            outcome = self.results.get(step, 0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return run

    def steps(self):
        return [step for step, _ in self.calls]

    def options(self, step):
        return next(options for name, options in self.calls if name == step)

    def test_runs_every_step_in_order_for_the_campaigns_businesses(self):
        result = run_command("run_pipeline", "--campaign-id", self.campaign.pk, "--fetch-limit", 5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.steps(), ["fetch", "research", "stakeholders", "verify", "generate"])
        self.assertEqual((self.options("fetch")["campaign_id"], self.options("fetch")["limit"]), (self.campaign.pk, 5))
        fetched = Business.objects.get(name="Fetched").pk
        for step in ("research", "stakeholders", "verify", "generate"):
            self.assertEqual(self.options(step)["business_id"], [fetched], step)  # never the other campaign's
        self.assertEqual(self.options("stakeholders")["min_score"], 50)
        self.assertIn("Nothing is sent", result.stdout)
        self.assertIn("Next: review the new emails", result.stdout)

    def test_partial_failures_continue_but_a_step_that_cannot_run_stops(self):
        self.results = {"research": 1}  # e.g. one website failed
        result = run_command("run_pipeline", "--campaign-id", self.campaign.pk)
        self.assertEqual(self.steps(), ["fetch", "research", "stakeholders", "verify", "generate"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Steps with some failures: Research and score websites", result.stdout)

        self.calls.clear()
        self.results = {"verify": CommandError("VERIFY_HELO is not set")}
        result = run_command("run_pipeline", "--campaign-id", self.campaign.pk, "--fetch-limit", 0)
        self.assertEqual(self.steps(), ["research", "stakeholders", "verify"])  # generate never runs
        self.assertIn("Stopped at step 3 (Verify emails): VERIFY_HELO is not set", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_skip_prompt_and_headed(self):
        run_command("run_pipeline", "--campaign-id", self.campaign.pk, "--skip", "research", "--skip", "verify",
                    "--headed", "--min-score", 70)
        self.assertEqual(self.steps(), ["fetch", "stakeholders", "generate"])
        self.assertTrue(self.options("fetch")["headed"])
        self.assertEqual(self.options("stakeholders")["min_score"], 70)

    def test_campaign_without_businesses_stops_instead_of_processing_everything(self):
        result = run_command("run_pipeline", "--campaign-id", self.campaign.pk, "--fetch-limit", 0)
        self.assertEqual(self.calls, [])
        self.assertIn("no businesses yet", result.stdout)

    def test_without_a_campaign_everything_is_processed_and_nothing_fetched(self):
        run_command("run_pipeline")
        self.assertEqual(self.steps(), ["research", "stakeholders", "verify", "generate"])
        self.assertIsNone(self.options("research")["business_id"])

    def test_invalid_input(self):
        for args, message in ((["--campaign-id", 999999], "does not exist"), (["--fetch-limit", -1], "negative"),
                              (["--email-prompt-id", 999999], "does not exist"), (["--skip", "send"], "invalid choice")):
            result = run_command("run_pipeline", *args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn(message, result.stderr, args)
        self.assertEqual(self.calls, [])


@override_settings(AUTH_TOKEN="pipe-key")
class PipelineStartTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("admin", "a@example.org", "pw")
        self.client.force_login(self.user)
        self.campaign = make_campaign(name="Mine")

    def test_campaign_page_button_and_action(self):
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change", args=[self.campaign.pk])).content.decode()
        self.assertIn("Run full pipeline", page)
        response = self.client.post(reverse("admin:pipeline_discoverycampaign_changelist"),
                                    {"action": "run_pipeline", "_selected_action": [self.campaign.pk]})
        form = self.client.get(response["Location"]).context["adminform"].form
        self.assertEqual(form.initial["command"], "run_pipeline")
        self.assertEqual(form.initial["arguments_text"], f"--campaign-id {self.campaign.pk} --fetch-limit 20")
        self.assertIn("Run the full pipeline", self.client.get(reverse("admin:index")).content.decode())

    def test_api(self):
        api = APIClient()
        api.credentials(HTTP_AUTH="pipe-key")
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            response = api.post(f"/api/campaigns/{self.campaign.pk}/run-pipeline/",
                                {"fetch_limit": 10, "skip": ["verify"]}, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(PipelineRun.objects.get().arguments,
                         ["--campaign-id", str(self.campaign.pk), "--fetch-limit", "10", "--skip", "verify"])
