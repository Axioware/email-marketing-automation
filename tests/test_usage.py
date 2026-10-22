"""Usage tracking: AI tokens and Hunter credits per call, with the action, business, prospect and run they were for."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from pipeline.models import PipelineRun, UsageRecord
from pipeline.services import llm, usage
from tests.support import run_command
from tests.test_hunter import HunterCase


def ai_response(prompt=1000, completion=200, reasoning=50):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}", refusal=None))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning)))


def ai_client(base_url="https://api.openai.com/v1/"):
    client = mock.Mock()
    client.base_url = base_url
    client.chat.completions.create.return_value = ai_response()
    return client


class UsageCase(HunterCase):
    def setUp(self):
        super().setUp()
        self.run = PipelineRun.objects.create(command="research_websites", arguments=[])


class RecordingTests(UsageCase):
    def test_ai_calls_record_their_tokens_and_what_they_were_for(self):
        with usage.track(action="research", business_id=self.clinic.pk, run_id=self.run.pk):
            llm.complete(ai_client(), model="gpt-6-luna", messages=[])
        llm.complete(ai_client("https://api.groq.com/openai/v1/"), model="openai/gpt-oss-120b", messages=[])
        first, second = UsageRecord.objects.order_by("id")
        self.assertEqual((first.provider, first.model, first.action, first.business, first.run,
                          first.input_tokens, first.output_tokens, first.reasoning_tokens),
                         ("openai", "gpt-6-luna", "research", self.clinic, self.run, 1000, 200, 50))
        self.assertEqual((second.provider, second.action, second.business), ("groq", "other", None))  # no context

    def test_hunter_searches_and_verifications_record_credits(self):
        run_command("find_stakeholders", "--business-id", self.clinic.pk)
        run_command("verify_emails", "--business-id", self.clinic.pk)
        search = UsageRecord.objects.get(action="decision_makers")
        self.assertEqual((search.provider, search.model, search.business, search.hunter_credits, search.detail),
                         ("hunter", "domain-search", self.clinic, Decimal("1.00"), "smile.pk (5 email(s))"))
        checks = UsageRecord.objects.filter(action="verification")
        self.assertEqual(checks.count(), 3)  # one per contact address
        self.assertEqual({c.hunter_credits for c in checks}, {Decimal("0.50")})
        self.assertEqual({c.business for c in checks}, {self.clinic})

    def test_a_failure_to_record_never_stops_the_work(self):
        with mock.patch("pipeline.models.UsageRecord.objects.create", side_effect=RuntimeError("db down")):
            response = llm.complete(ai_client(), model="m", messages=[])
        self.assertEqual(response.usage.prompt_tokens, 1000)
        self.assertFalse(UsageRecord.objects.exists())

    def test_a_run_attributes_its_calls(self):
        from pipeline import jobs

        run = PipelineRun.objects.create(command="find_stakeholders",
                                         arguments=["--business-id", str(self.clinic.pk)])
        jobs.execute_run(run.pk)
        self.assertEqual(list(UsageRecord.objects.values_list("run_id", "action")), [(run.pk, "decision_makers")])


class UsagePageTests(UsageCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser("u", "u@example.org", "pw")
        self.client.force_login(self.user)
        with usage.track(action="research", business_id=self.clinic.pk):
            usage.record("openai", "gpt-6-luna", input_tokens=20000, output_tokens=1500, reasoning_tokens=300)
        with usage.track(action="email_scoring", business_id=self.clinic.pk):
            usage.record("openai", "gpt-6-luna", input_tokens=400, output_tokens=120)
        with usage.track(action="verification", business_id=self.clinic.pk):
            usage.record_hunter_verification("owner@smile.pk")
        old = UsageRecord.objects.create(provider="openai", model="gpt-5-mini", action="research",
                                         input_tokens=9999, output_tokens=1)
        UsageRecord.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))

    def test_list_totals_and_date_range(self):
        url = reverse("admin:pipeline_usagerecord_changelist")
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Usage: AI tokens and Hunter credits")
        self.assertEqual(page.context["totals"]["calls"], 4)
        self.assertEqual(page.context["totals"]["input"], 30399)
        today = timezone.localdate().isoformat()
        recent = self.client.get(url, {"from": today, "to": today})
        self.assertEqual(recent.context["totals"]["calls"], 3)  # the 30-day-old one is outside the range
        self.assertEqual(recent.context["totals"]["input"], 20400)
        self.assertEqual(recent.context["totals"]["credits"], Decimal("0.50"))
        self.assertContains(recent, "Website research (qualification)")
        hunter_only = self.client.get(url, {"from": today, "provider": "hunter"})
        self.assertEqual(hunter_only.context["totals"]["calls"], 1)
        by_business = self.client.get(url, {"q": "Smile Studio"})
        self.assertEqual(by_business.context["totals"]["calls"], 3)
        self.assertIn(url, self.client.get(reverse("admin:index")).content.decode())  # in the sidebar
        self.assertContains(page, "estimate: a domain search 1 credit per 10 emails it returns, a verification 0.5")

    @override_settings(AUTH_TOKEN="k" * 20)
    def test_api(self):
        api = APIClient()
        api.credentials(HTTP_AUTH="k" * 20)
        today = timezone.localdate().isoformat()
        listed = api.get("/api/usage/", {"from": today}).json()
        self.assertEqual(listed["count"], 3)
        summary = api.get("/api/usage/summary/", {"from": today, "action": "research"}).json()
        self.assertEqual((summary["totals"]["calls"], summary["totals"]["input_tokens"]), (1, 20000))
        self.assertEqual(summary["by_action_and_model"][0]["model"], "gpt-6-luna")
