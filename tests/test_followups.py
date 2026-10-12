"""Follow-up sequences: A/B assignment, when follow-ups are due, prompt choice, replies, sending, admin and API."""
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from pipeline import review
from pipeline.models import Business, CampaignPrompt, Email, EmailPrompt, FollowUp, PipelineRun, Prospect
from pipeline.services import followups, generation, sending
from tests.fake_smtp import FakeSmtpServer
from tests.support import BASE, CAMPAIGN_INLINE, make_campaign, make_email, review_env, run_command
from tests.test_generate_emails import FakeLLM


class FollowUpCase(TestCase):
    def setUp(self):
        self.smtp = FakeSmtpServer()
        self.llm = FakeLLM()
        self.addCleanup(self.smtp.close)
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(os.environ, review_env(self.smtp, self.llm))
        env.start()
        self.addCleanup(env.stop)
        self.user = get_user_model().objects.create_superuser("admin", "a@example.org", "pw")
        self.client.force_login(self.user)
        self.campaign = make_campaign(name="Clinics")
        self.campaign_prompt = CampaignPrompt.objects.create(name="Clinics prompt", prompt="RULES")
        self.campaign.campaign_prompt = self.campaign_prompt
        self.campaign.save()
        self.prompt("First A", 0, "a", is_default=True)
        self.prompt("First B", 0, "b")
        self.prompt("F1 A opened", 1, "a", opened=True)
        self.prompt("F1 A any", 1, "a")
        self.prompt("F1 default", 1, "c", is_default=True)
        self.prompt("F2", 2, "a", is_default=True)
        FollowUp.objects.create(campaign=self.campaign, sequence=1, days_after_previous=3)
        FollowUp.objects.create(campaign=self.campaign, sequence=2, days_after_previous=7)

    def prompt(self, name, sequence, variation, opened=None, is_default=False):
        return EmailPrompt.objects.create(campaign_prompt=self.campaign_prompt, name=name, sequence=sequence,
                                          variation=variation, opened=opened, is_default=is_default,
                                          prompt=name.upper())

    def sent_email(self, days_ago=4, variation="a", opened=False, **email_fields):
        """A first email sent `days_ago` days ago to a contacted prospect."""
        email = make_email("opened" if opened else "sent", campaign=self.campaign, **email_fields)
        Email.objects.filter(pk=email.pk).update(sent_at=timezone.now() - timedelta(days=days_ago),
                                                 open_count=1 if opened else 0)
        Prospect.objects.filter(pk=email.prospect_id).update(outreach_status="contacted", variation=variation)
        email.refresh_from_db()
        return email

    def write(self, *args):
        return run_command("generate_followups", *args)

    def followup_of(self, email, step=2):
        return Email.objects.filter(prospect_id=email.prospect_id, sequence_step=step).first()


class AssignmentTests(FollowUpCase):
    def test_first_emails_split_prospects_equally_and_keep_the_variation(self):
        emails = [make_email(campaign=self.campaign) for _ in range(4)]
        for email in emails:
            email.delete()  # four ready prospects without an email
        result = run_command("generate_emails")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        variations = sorted(Prospect.objects.filter(business__discovery_campaign=self.campaign)
                            .values_list("variation", flat=True))
        self.assertEqual(variations, ["a", "a", "b", "b"])
        prompts = {e.prospect.variation: e.email_prompt.name for e in Email.objects.select_related("prospect", "email_prompt")}
        self.assertEqual(prompts, {"a": "First A", "b": "First B"})
        before = dict(Prospect.objects.values_list("id", "variation"))
        run_command("generate_emails", "--regenerate")
        self.assertEqual(dict(Prospect.objects.values_list("id", "variation")), before)  # never reassigned

    def test_variation_balance_counts_existing_prospects(self):
        self.sent_email(variation="a")
        self.sent_email(variation="a")
        email = make_email(campaign=self.campaign)
        row = generation.prospect_rows(Prospect.objects.filter(pk=email.prospect_id))[0]
        self.assertEqual(generation.assign_variation(row), "b")
        self.assertEqual(Prospect.objects.get(pk=email.prospect_id).variation, "b")


class DueTests(FollowUpCase):
    def test_not_due_before_the_delay_and_written_after_it(self):
        early = self.sent_email(days_ago=2)
        due = self.sent_email(days_ago=4)
        result = self.write()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.followup_of(early))
        follow_up = self.followup_of(due)
        self.assertEqual((follow_up.status, follow_up.sequence_step, follow_up.recipient),
                         ("in_review", 2, due.recipient))
        self.assertNotEqual(follow_up.tracking_token, due.tracking_token)
        self.assertIn(f"{BASE}/{follow_up.tracking_token}", follow_up.body_html)
        payload = self.llm.payloads[-1]
        self.assertEqual(payload["follow_up"], {"number": 1, "previous_email_opened": False})
        self.assertEqual([e["subject"] for e in payload["previous_emails"]], [due.subject])
        self.assertIn("1 follow-up(s) saved for review", result.stdout)

    def test_prompt_depends_on_variation_and_opened(self):
        not_opened = self.sent_email(variation="a")
        opened = self.sent_email(variation="a", opened=True)
        other = self.sent_email(variation="b")  # no step-1 prompt for b: the step's default
        self.write()
        self.assertEqual(self.followup_of(not_opened).email_prompt.name, "F1 A any")
        self.assertEqual(self.followup_of(opened).email_prompt.name, "F1 A opened")
        self.assertEqual(self.followup_of(other).email_prompt.name, "F1 default")
        self.assertIn("RULES\n\nF1 A OPENED", self.llm.system_prompts)

    def test_no_follow_up_after_a_reply_or_do_not_contact_or_twice(self):
        replied = self.sent_email()
        dnc = self.sent_email()
        Prospect.objects.filter(pk=dnc.prospect_id).update(do_not_contact=True)
        review.mark_replied(replied)
        done = self.sent_email()
        self.write()
        self.assertIsNone(self.followup_of(replied))
        self.assertIsNone(self.followup_of(dnc))
        self.assertIsNotNone(self.followup_of(done))
        self.llm.payloads.clear()
        self.assertIn("No follow-ups are due", self.write().stdout)  # the follow-up exists (in review)

    def test_a_reply_from_anyone_at_the_business_stops_all_its_prospects(self):
        first = self.sent_email()
        second_prospect = Prospect.objects.create(business=first.business, contact=first.contact,
                                                  email="other@clinic.org", email_status="deliverable",
                                                  outreach_status="contacted", variation="a")
        second = Email.objects.create(prospect=second_prospect, business=first.business, contact=first.contact,
                                      recipient="other@clinic.org", subject="s", body_text="b", body_html="h",
                                      tracking_token=generation.new_tracking_token(), status="sent",
                                      sent_at=timezone.now() - timedelta(days=5))
        review.mark_replied(first)
        self.assertEqual(followups.find_due({}), [])
        self.assertIsNone(self.followup_of(second))

    def test_second_follow_up_uses_its_own_delay_and_prompt(self):
        first = self.sent_email(days_ago=20)
        self.write()
        follow_up = self.followup_of(first)
        Email.objects.filter(pk=follow_up.pk).update(status="sent", sent_at=timezone.now() - timedelta(days=6))
        self.assertIn("No follow-ups are due", self.write().stdout)  # 7 days needed
        Email.objects.filter(pk=follow_up.pk).update(sent_at=timezone.now() - timedelta(days=8))
        self.write()
        second = self.followup_of(first, step=3)
        self.assertEqual(second.email_prompt.name, "F2")
        self.assertEqual(len(self.llm.payloads[-1]["previous_emails"]), 2)
        Email.objects.filter(pk=second.pk).update(status="sent", sent_at=timezone.now() - timedelta(days=30))
        self.assertIn("No follow-ups are due", self.write().stdout)  # the campaign has only two follow-ups

    def test_unsent_or_failed_latest_email_is_not_followed_up(self):
        for status in ("in_review", "approved", "failed", "replied"):
            email = self.sent_email()
            Email.objects.filter(pk=email.pk).update(status=status)
        self.assertEqual(followups.find_due({}), [])

    def test_missing_prompt_skips_and_reports(self):
        EmailPrompt.objects.filter(sequence=1).delete()
        email = self.sent_email()
        result = self.write()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no email prompt for follow-up 1", result.stdout)
        self.assertIsNone(self.followup_of(email))

    def test_filters_dry_run_and_chosen_prompt(self):
        first, second = self.sent_email(), self.sent_email()
        result = self.write("--dry-run")
        self.assertIn("follow-up(s) written (dry run, not saved)", result.stdout)
        self.assertEqual(Email.objects.filter(sequence_step=2).count(), 0)
        chosen = EmailPrompt.objects.get(name="F2")
        self.write("--prospect-id", second.prospect_id, "--email-prompt-id", chosen.pk)
        self.assertIsNone(self.followup_of(first))
        self.assertEqual(self.followup_of(second).email_prompt, chosen)


class ReplyAndSendTests(FollowUpCase):
    def approved_follow_up(self):
        first = self.sent_email()
        self.write()
        follow_up = self.followup_of(first)
        Email.objects.filter(pk=follow_up.pk).update(status="approved")
        follow_up.refresh_from_db()
        return first, follow_up

    def test_follow_up_to_a_contacted_prospect_is_sendable(self):
        _, follow_up = self.approved_follow_up()
        self.assertIn(follow_up.pk, set(sending.sendable_emails().values_list("id", flat=True)))
        self.assertIn("Sent to", review.send(follow_up, follow_up.recipient))
        self.assertEqual(Email.objects.get(pk=follow_up.pk).status, "sent")

    def test_reply_blocks_sending_even_after_approval(self):
        first, follow_up = self.approved_follow_up()
        response = self.client.post(reverse("admin:pipeline_email_replied", args=[first.pk]), follow=True)
        self.assertContains(response, "No more follow-ups go to this business")
        first.refresh_from_db()
        self.assertEqual(first.status, "replied")
        self.assertIsNotNone(Business.objects.get(pk=first.business_id).replied_at)
        self.assertNotIn(follow_up.pk, set(sending.sendable_emails().values_list("id", flat=True)))
        with self.assertRaisesMessage(review.ReviewError, "replied"):
            review.send(follow_up, follow_up.recipient)
        message = {field: getattr(follow_up, field) for field in sending.EMAIL_FIELDS}
        self.assertEqual(sending.send_one(message, sending.smtp_config(), sending.Mailer(sending.smtp_config()))[0], "skipped")
        self.assertEqual(self.smtp.messages, [])

    def test_mark_replied_only_for_sent_emails(self):
        email = make_email(campaign=self.campaign)
        with self.assertRaises(review.ReviewError):
            review.mark_replied(email)

    def test_mark_replied_from_prospect_and_business_lists(self):
        first = self.sent_email()
        self.client.post(reverse("admin:pipeline_prospect_changelist"),
                         {"action": "mark_replied", "_selected_action": [first.prospect_id]})
        self.assertEqual(Email.objects.get(pk=first.pk).status, "replied")
        other = self.sent_email()
        self.client.post(reverse("admin:pipeline_business_changelist"),
                         {"action": "mark_replied", "_selected_action": [other.business_id]})
        self.assertIsNotNone(Business.objects.get(pk=other.business_id).replied_at)
        self.client.post(reverse("admin:pipeline_business_changelist"),
                         {"action": "clear_replied", "_selected_action": [other.business_id]})
        self.assertIsNone(Business.objects.get(pk=other.business_id).replied_at)

    def test_replied_status_survives_opens(self):
        first = self.sent_email()
        review.mark_replied(first)
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("select record_email_open(%s, 'Mail')", [first.tracking_token])
        self.assertEqual(Email.objects.get(pk=first.pk).status, "replied")

    def test_regenerate_a_follow_up(self):
        first = self.sent_email(opened=True)
        self.write()
        follow_up = self.followup_of(first)
        old_subject = follow_up.subject
        self.client.post(reverse("admin:pipeline_email_regenerate", args=[follow_up.pk]))
        follow_up.refresh_from_db()
        self.assertEqual((follow_up.sequence_step, follow_up.status), (2, "in_review"))
        self.assertNotEqual(follow_up.subject, old_subject)
        self.assertEqual(follow_up.email_prompt.name, "F1 A opened")


class AdminAndApiTests(FollowUpCase):
    def test_campaign_inline_and_missing_prompt_warning(self):
        FollowUp.objects.create(campaign=self.campaign, sequence=3, days_after_previous=10)
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change", args=[self.campaign.pk]))
        content = page.content.decode()
        self.assertIn("follow_ups-0-days_after_previous", content)
        self.assertIn("Follow-up 3 has no email prompt", content)
        response = self.client.post(reverse("admin:pipeline_discoverycampaign_add"), {
            **CAMPAIGN_INLINE, "name": "With follow-ups", "follow_ups-TOTAL_FORMS": "2",
            "follow_ups-0-sequence": "1", "follow_ups-0-days_after_previous": "3",
            "follow_ups-1-sequence": "2", "follow_ups-1-days_after_previous": "7"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(FollowUp.objects.filter(campaign__name="With follow-ups")
                              .values_list("sequence", "days_after_previous")), [(1, 3), (2, 7)])

    def test_email_prompt_form_rules(self):
        url = reverse("admin:pipeline_emailprompt_add")
        base = {"campaign_prompt": self.campaign_prompt.pk, "variation": "b", "prompt": generation.DEFAULT_EMAIL_PROMPT}
        response = self.client.post(url, {**base, "name": "Bad", "sequence": "0", "opened": "true"})
        self.assertContains(response, "Only follow-ups")
        self.client.post(url, {**base, "name": "F3", "sequence": "3", "opened": "unknown"})
        self.assertEqual(EmailPrompt.objects.get(name="F3").prompt, generation.DEFAULT_FOLLOW_UP_PROMPT)

    def test_email_list_filters_and_dashboard(self):
        first = self.sent_email()
        self.write()
        url = reverse("admin:pipeline_email_changelist")
        self.assertEqual(self.client.get(url, {"sequence_step": "2"}).context["cl"].result_count, 1)
        self.assertEqual(self.client.get(url, {"prospect__variation": "a"}).context["cl"].result_count, 2)
        page = self.client.get(reverse("admin:pipeline_email_change", args=[self.followup_of(first).pk])).content.decode()
        self.assertIn("Follow-up 1", page)
        another = self.sent_email()
        review.mark_replied(first)
        dashboard = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("1 follow-up(s) are due", dashboard)
        self.assertIn("Results by step and variation", dashboard)
        self.assertTrue(another)

    @override_settings(AUTH_TOKEN="fu-key")
    def test_api(self):
        api = APIClient()
        api.credentials(HTTP_AUTH="fu-key")
        created = api.post("/api/follow-ups/", {"campaign": self.campaign.pk, "sequence": 3, "days_after_previous": 14},
                           format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(api.post("/api/follow-ups/", {"campaign": self.campaign.pk, "sequence": 3,
                                                       "days_after_previous": 1}, format="json").status_code, 400)
        campaign = api.get(f"/api/campaigns/{self.campaign.pk}/").json()
        self.assertEqual([f["sequence"] for f in campaign["follow_ups"]], [1, 2, 3])
        first = self.sent_email()
        response = api.post(f"/api/emails/{first.pk}/mark-replied/")
        self.assertEqual(response.json()["email"]["status"], "replied")
        other = self.sent_email()
        api.post(f"/api/businesses/{other.business_id}/mark-replied/")
        self.assertIsNotNone(Business.objects.get(pk=other.business_id).replied_at)
        third = self.sent_email()
        api.post(f"/api/prospects/{third.prospect_id}/mark-replied/")
        self.assertEqual(Email.objects.get(pk=third.pk).status, "replied")
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            run = api.post("/api/runs/", {"command": "generate_followups", "options": {"campaign_id": self.campaign.pk}},
                           format="json")
        self.assertEqual(run.status_code, 202, run.content)
        self.assertEqual(PipelineRun.objects.get().arguments, ["--campaign-id", str(self.campaign.pk)])
