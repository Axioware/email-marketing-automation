"""Excluded address patterns and the AI email score (Score emails), against a fake verifier and a fake AI."""
import json
import os
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse

from pipeline.models import BusinessContact, PipelineRun, Prospect, SendingSettings
from pipeline.services import email_scoring
from tests.support import run_command
from tests.test_hunter import HunterCase, person


class FakeAI:
    """An OpenAI-style client: scores by the address's local part, records what it was sent."""

    def __init__(self, scores):
        self.scores, self.requests = scores, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        return self

    def create(self, **request):
        self.requests.append(request)
        address = json.loads(request["messages"][1]["content"])["address"]
        score = self.scores[address.partition("@")[0]]
        content = json.dumps({"score": score, "reasoning": f"{address} scored {score}."})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None))])


class PatternTests(HunterCase):
    def test_patterns_match_case_insensitively(self):
        excluded = email_scoring.patterns("Info@*\n\n *@gmail.com \nbusiness@*")
        self.assertEqual(excluded, ["info@*", "*@gmail.com", "business@*"])
        self.assertEqual(email_scoring.matching_pattern("INFO@Clinic.pk", excluded), "info@*")
        self.assertEqual(email_scoring.matching_pattern("dr.ali@gmail.com", excluded), "*@gmail.com")
        self.assertIsNone(email_scoring.matching_pattern("ali@clinic.pk", excluded))
        self.assertIn("business@*", email_scoring.patterns())  # the defaults


class ScoringCase(HunterCase):
    require_score = True

    def setUp(self):
        super().setUp()
        self.hunter.domains["smile.pk"]["emails"].append(
            person("info2@smile.pk", "Front", "Office", "Office Manager", confidence=99))
        self.hunter.statuses.update({"info2": {"status": "valid"}, "bilal": {"status": "valid"}})
        run_command("find_stakeholders", "--business-id", self.clinic.pk)
        SendingSettings.objects.filter(pk=1).update(excluded_email_patterns="info*@*\nbusiness@*")
        self.hunter.calls.clear()

    def verify(self):
        return run_command("verify_emails", "--business-id", self.clinic.pk)

    def score(self, scores, *args):
        self.ai = FakeAI(scores)
        with mock.patch("pipeline.services.email_scoring.make_llm_client", return_value=(self.ai, "gpt-5-mini", "openai")):
            return run_command("score_emails", *args)

    def prospect(self, address):
        return Prospect.objects.get(email=address)


class VerificationTests(ScoringCase):
    def test_excluded_addresses_are_never_checked_and_verified_ones_wait_for_a_score(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checked = [params["email"] for _, params in self.hunter.calls]
        self.assertNotIn("info2@smile.pk", checked)  # no credit spent on it
        excluded = self.prospect("info2@smile.pk")
        self.assertEqual((excluded.outreach_status, excluded.verdict), ("rejected", "excluded"))
        self.assertIn("matches the pattern info*@*", excluded.verification_note)
        owner = self.prospect("owner@smile.pk")
        self.assertEqual((owner.email_status, owner.outreach_status), ("deliverable", "pending"))
        self.assertIn("waiting for an AI score", result.stdout)

    def test_without_required_scoring_deliverable_is_ready_at_once(self):
        SendingSettings.objects.filter(pk=1).update(email_scoring_enabled=False)
        self.verify()
        self.assertEqual(self.prospect("owner@smile.pk").outreach_status, "ready")


class ScoreEmailsTests(ScoringCase):
    def setUp(self):
        super().setUp()
        self.verify()

    def test_scores_gate_who_is_ready_to_email(self):
        result = self.score({"owner": 92, "bilal": 35, "manager": 88})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        owner, bilal, manager = (self.prospect(f"{name}@smile.pk") for name in ("owner", "bilal", "manager"))
        self.assertEqual((owner.email_score, owner.outreach_status), (92, "ready"))
        self.assertEqual(owner.email_score_reason, "owner@smile.pk scored 92.")
        self.assertEqual((bilal.email_score, bilal.outreach_status), (35, "needs_review"))  # below 60
        self.assertEqual((manager.email_score, manager.outreach_status), (88, "needs_review"))  # risky: a person decides
        self.assertNotIn("info2", " ".join(r["messages"][1]["content"] for r in self.ai.requests))  # excluded: not sent
        request = self.ai.requests[0]
        self.assertEqual(request["messages"][0]["content"], SendingSettings.current().email_scoring_prompt)
        self.assertEqual(request["reasoning_effort"], "low")  # the model's settings (no temperature on gpt-5)
        sent = json.loads(request["messages"][1]["content"])
        self.assertEqual(sorted(sent), ["address", "business", "contact", "other_addresses_at_this_business",
                                        "verification"])
        self.assertEqual(sent["contact"]["job_title"], "Owner & Principal Dentist")
        self.assertIn("Done: 1 ready to email, 2 need review", result.stdout)

    def test_scoring_uses_its_own_model_from_settings(self):
        SendingSettings.objects.filter(pk=1).update(email_scoring_model="gpt-5-nano")
        result = self.score({"owner": 92, "bilal": 35, "manager": 88})
        self.assertIn("with openai model gpt-5-nano", result.stdout)
        self.assertEqual({request["model"] for request in self.ai.requests}, {"gpt-5-nano"})
        SendingSettings.objects.filter(pk=1).update(email_scoring_model="")
        self.score({"owner": 92, "bilal": 35, "manager": 88}, "--rescore")
        self.assertEqual({request["model"] for request in self.ai.requests}, {"gpt-5-mini"})  # research's model

    def test_scored_ones_are_not_paid_for_again_unless_rescored(self):
        self.score({"owner": 92, "bilal": 35, "manager": 88})
        self.assertIn("no address waits for a score", self.score({}).stdout)
        SendingSettings.objects.filter(pk=1).update(email_min_score=30)
        self.score({"owner": 92, "bilal": 50, "manager": 88}, "--rescore")
        self.assertEqual(self.prospect("bilal@smile.pk").outreach_status, "ready")

    def test_a_pattern_added_later_disqualifies_addresses_verified_before(self):
        SendingSettings.objects.filter(pk=1).update(excluded_email_patterns="bilal@*")
        result = self.score({"owner": 92, "manager": 88})
        self.assertIn("bilal@smile.pk: Excluded: matches the pattern bilal@*", result.stdout)
        self.assertEqual(self.prospect("bilal@smile.pk").outreach_status, "rejected")

    def test_reverifying_keeps_the_score_decision(self):
        self.score({"owner": 92, "bilal": 35, "manager": 88})
        run_command("verify_emails", "--business-id", self.clinic.pk, "--recheck")
        self.assertEqual(self.prospect("owner@smile.pk").outreach_status, "ready")  # not back to pending

    def test_dry_run_and_missing_ai_key(self):
        self.score({"owner": 92, "bilal": 35, "manager": 88}, "--dry-run")
        self.assertIsNone(self.prospect("owner@smile.pk").email_score)
        with mock.patch("pipeline.services.email_scoring.make_llm_client", return_value=None):
            self.assertIn("to score emails", run_command("score_emails").stderr)

    def test_admin_settings_dashboard_and_actions(self):
        user = get_user_model().objects.create_superuser("s", "s@example.org", "pw")
        self.client.force_login(user)
        page = self.client.get(reverse("admin:pipeline_sendingsettings_change", args=[1]))
        for text in ("Email scoring", "Require an AI score", "Minimum email score", "info*@*",
                     "decision maker"):
            self.assertContains(page, text)
        self.assertContains(self.client.get(reverse("admin:index")), "wait for an AI score")
        prospect = self.prospect("owner@smile.pk")
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("admin:pipeline_prospect_changelist"),
                                        {"action": "score_emails", "_selected_action": [prospect.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertIn("score_emails", self.client.get(response["Location"]).content.decode())
        self.score({"owner": 92, "bilal": 35, "manager": 88})
        listing = self.client.get(reverse("admin:pipeline_prospect_changelist")).content.decode()
        self.assertIn("owner@smile.pk scored 92.", listing)  # the reasoning, on the score badge
