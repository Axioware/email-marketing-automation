"""Follow-up sequences: A/B assignment over initial-email templates, when follow-ups are written, template per step,
replies (per prospect), admin and API."""
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from pipeline import review
from pipeline.models import Email, EmailPrompt, FollowUp, PipelineRun, Prospect
from pipeline.services import followups, generation
from tests.support import BASE, CAMPAIGN_INLINE, make_campaign, make_email, make_email_prompt, run_command


class FollowUpCase(TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"EMAIL_TRACKING_BASE_URL": BASE, "SENDER_NAME": "Sender"})
        env.start()
        self.addCleanup(env.stop)
        self.user = get_user_model().objects.create_superuser("admin", "a@example.org", "pw")
        self.client.force_login(self.user)
        self.first_a = make_email_prompt("First A", "FIRST A")
        self.first_b = make_email_prompt("First B", "FIRST B")
        self.f1 = make_email_prompt("F1", "F1 RULES", kind="follow_up")
        self.f2 = make_email_prompt("F2", "F2 RULES", kind="follow_up")
        self.campaign = make_campaign(name="Clinics", initial_prompts=[self.first_a, self.first_b])
        FollowUp.objects.create(campaign=self.campaign, sequence=1, days_after_previous=3, email_prompt=self.f1)
        FollowUp.objects.create(campaign=self.campaign, sequence=2, days_after_previous=7, email_prompt=self.f2)

    def sent_email(self, days_ago=1, **email_fields):
        """An initial email sent `days_ago` days ago to a contacted prospect (A/B group: First A)."""
        email = make_email("sent", campaign=self.campaign, **email_fields)
        Email.objects.filter(pk=email.pk).update(sent_at=timezone.now() - timedelta(days=days_ago),
                                                 email_prompt=self.first_a)
        Prospect.objects.filter(pk=email.prospect_id).update(outreach_status="contacted",
                                                             initial_email_prompt=self.first_a)
        email.refresh_from_db()
        return email

    def write(self, *args):
        return run_command("generate_followups", *args)

    def followup_of(self, email, step=2):
        return Email.objects.filter(prospect_id=email.prospect_id, sequence_step=step).first()


class AssignmentTests(FollowUpCase):
    def test_initial_emails_split_prospects_equally_and_keep_the_prompt(self):
        for _ in range(4):
            make_email(campaign=self.campaign).delete()  # four ready prospects without an email
        result = run_command("generate_emails")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        groups = sorted(Prospect.objects.filter(business__discovery_campaign=self.campaign)
                        .values_list("initial_email_prompt__name", flat=True))
        self.assertEqual(groups, ["First A", "First A", "First B", "First B"])
        for email in Email.objects.select_related("prospect"):
            self.assertEqual(email.email_prompt_id, email.prospect.initial_email_prompt_id)
        self.assertEqual(sorted(email.body_text.split("\n\n")[1].split(" about")[0] for email in Email.objects.all()),
                         ["FIRST A", "FIRST A", "FIRST B", "FIRST B"])  # each filled from its own template
        before = dict(Prospect.objects.values_list("id", "initial_email_prompt"))
        run_command("generate_emails", "--regenerate")
        self.assertEqual(dict(Prospect.objects.values_list("id", "initial_email_prompt")), before)  # never reassigned

    def test_balance_counts_existing_prospects(self):
        self.sent_email()
        self.sent_email()  # two prospects already in First A
        email = make_email(campaign=self.campaign)
        row = generation.prospect_rows(Prospect.objects.filter(pk=email.prospect_id))[0]
        self.assertEqual(generation.assign_initial_prompt(row), self.first_b.pk)
        self.assertEqual(Prospect.objects.get(pk=email.prospect_id).initial_email_prompt, self.first_b)

    def test_campaign_without_initial_prompts_is_skipped(self):
        campaign = make_campaign(name="Bare", initial_prompts=[])
        make_email(campaign=campaign).delete()
        result = run_command("generate_emails")
        self.assertIn("its campaign has no initial-email template", result.stdout)
        self.assertFalse(Email.objects.exists())


class DueTests(FollowUpCase):
    def test_written_right_after_the_previous_email_was_sent(self):
        just_sent = self.sent_email(days_ago=0)
        result = self.write()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        follow_up = self.followup_of(just_sent)
        self.assertEqual((follow_up.status, follow_up.sequence_step, follow_up.recipient, follow_up.email_prompt),
                         ("in_review", 2, just_sent.recipient, self.f1))
        self.assertNotEqual(follow_up.tracking_token, just_sent.tracking_token)
        self.assertNotIn("<img", follow_up.body_html)
        self.assertEqual(follow_up.subject, f"Re: {just_sent.subject}")  # {{previous_subject}}
        self.assertIn("F1 RULES about", follow_up.body_text)  # the step's template
        self.assertIn("1 follow-up(s) saved for review", result.stdout)

    def test_no_follow_up_after_a_reply_do_not_contact_a_stop_or_twice(self):
        replied = self.sent_email()
        dnc = self.sent_email()
        Prospect.objects.filter(pk=dnc.prospect_id).update(do_not_contact=True)
        stopped = self.sent_email()
        Prospect.objects.filter(pk=stopped.prospect_id).update(sequence_stopped_at=timezone.now())
        review.mark_replied(replied)
        done = self.sent_email()
        self.write()
        self.assertIsNone(self.followup_of(replied))
        self.assertIsNone(self.followup_of(dnc))
        self.assertIsNone(self.followup_of(stopped))
        self.assertIsNotNone(self.followup_of(done))
        self.assertIn("No follow-ups are due", self.write().stdout)  # the follow-up exists (in review)

    def test_each_prospect_has_its_own_sequence(self):
        first = self.sent_email()
        colleague = Prospect.objects.create(business=first.business, contact=first.contact, email="other@clinic.org",
                                            email_status="deliverable", outreach_status="contacted")
        second = Email.objects.create(prospect=colleague, business=first.business, contact=first.contact,
                                      recipient="other@clinic.org", subject="s", body_text="b", body_html="h",
                                      tracking_token=generation.new_tracking_token(), status="sent",
                                      sent_at=timezone.now() - timedelta(days=1))
        review.mark_replied(first)
        self.assertEqual([item["prospect_id"] for item in followups.find_due({})], [colleague.pk])
        self.write()
        self.assertIsNotNone(self.followup_of(second))

    def test_second_follow_up_uses_its_own_prompt_and_the_sequence_ends(self):
        first = self.sent_email()
        self.write()
        follow_up = self.followup_of(first)
        self.assertIn("No follow-ups are due", self.write().stdout)  # follow-up 1 not sent yet
        Email.objects.filter(pk=follow_up.pk).update(status="sent", sent_at=timezone.now())
        self.write()
        second = self.followup_of(first, step=3)
        self.assertEqual(second.email_prompt, self.f2)
        self.assertEqual(second.subject, f"Re: Re: {first.subject}")  # follow-up 1's subject was "Re: ..."
        self.assertIn("F2 RULES about", second.body_text)
        Email.objects.filter(pk=second.pk).update(status="sent", sent_at=timezone.now())
        self.assertIn("No follow-ups are due", self.write().stdout)  # the campaign has two follow-ups

    def test_unsent_or_failed_latest_email_is_not_followed_up(self):
        for status in ("in_review", "approved", "scheduled", "sending", "failed", "replied"):
            email = self.sent_email()
            Email.objects.filter(pk=email.pk).update(status=status)
        self.assertEqual(followups.find_due({}), [])

    def test_a_step_without_a_prompt_is_never_due(self):
        FollowUp.objects.filter(sequence=1).update(email_prompt=None)
        email = self.sent_email()
        self.assertEqual(followups.find_due({}), [])
        self.write()
        self.assertIsNone(self.followup_of(email))
        chosen = self.write("--email-prompt-id", self.f2.pk)  # a chosen prompt still writes it
        self.assertEqual(chosen.returncode, 0, chosen.stdout)
        self.assertEqual(self.followup_of(email).email_prompt, self.f2)

    def test_filters_dry_run_and_chosen_prompt(self):
        first, second = self.sent_email(), self.sent_email()
        result = self.write("--dry-run")
        self.assertIn("follow-up(s) written (dry run, not saved)", result.stdout)
        self.assertEqual(Email.objects.filter(sequence_step=2).count(), 0)
        self.write("--prospect-id", second.prospect_id, "--email-prompt-id", self.f2.pk)
        self.assertIsNone(self.followup_of(first))
        self.assertEqual(self.followup_of(second).email_prompt, self.f2)


class ReplyTests(FollowUpCase):
    def test_a_reply_stops_that_prospect_and_cancels_what_waits(self):
        first = self.sent_email()
        self.write()
        follow_up = self.followup_of(first)
        response = self.client.post(reverse("admin:pipeline_email_replied", args=[first.pk]), follow=True)
        self.assertContains(response, "No more emails go to this prospect")
        first.refresh_from_db()
        follow_up.refresh_from_db()
        prospect = Prospect.objects.get(pk=first.prospect_id)
        self.assertEqual((first.status, follow_up.status), ("replied", "rejected"))
        self.assertEqual(prospect.sequence_stop_reason, "replied")
        self.assertIsNotNone(prospect.sequence_stopped_at)
        self.assertIsNone(first.business.replied_at)  # the business's other people are not affected
        self.assertFalse(review.prospect_ready(follow_up))

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
        self.assertEqual(Email.objects.get(pk=other.pk).status, "replied")

    def test_replied_status_survives_opens(self):
        first = self.sent_email()
        review.mark_replied(first)
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("select record_email_open(%s, 'Mail')", [first.tracking_token])
        self.assertEqual(Email.objects.get(pk=first.pk).status, "replied")

    def test_regenerate_a_follow_up(self):
        first = self.sent_email()
        self.write()
        follow_up = self.followup_of(first)
        self.f1.subject = "Following up: {{previous_subject}}"
        self.f1.save()
        self.client.post(reverse("admin:pipeline_email_regenerate", args=[follow_up.pk]))
        follow_up.refresh_from_db()
        self.assertEqual((follow_up.sequence_step, follow_up.status, follow_up.email_prompt), (2, "in_review", self.f1))
        self.assertEqual(follow_up.subject, f"Following up: {first.subject}")


class AdminAndApiTests(FollowUpCase):
    def test_campaign_inline_and_missing_prompt_warning(self):
        FollowUp.objects.create(campaign=self.campaign, sequence=3, days_after_previous=10)
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change", args=[self.campaign.pk]))
        content = page.content.decode()
        self.assertIn("follow_ups-0-email_prompt", content)
        self.assertIn("Follow-up 3 has no template", content)
        options = page.context["inline_admin_formsets"][0].formset.forms[0].fields["email_prompt"].queryset
        self.assertEqual(set(options), {self.f1, self.f2})  # follow-up prompts only
        response = self.client.post(reverse("admin:pipeline_discoverycampaign_add"), {
            **CAMPAIGN_INLINE, "name": "With follow-ups", "initial_email_prompts": [self.first_a.pk],
            "follow_ups-TOTAL_FORMS": "2",
            "follow_ups-0-sequence": "1", "follow_ups-0-days_after_previous": "3", "follow_ups-0-email_prompt": self.f1.pk,
            "follow_ups-1-sequence": "2", "follow_ups-1-days_after_previous": "7", "follow_ups-1-email_prompt": self.f2.pk})
        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assertEqual(list(FollowUp.objects.filter(campaign__name="With follow-ups")
                              .values_list("sequence", "days_after_previous", "email_prompt__name")),
                         [(1, 3, "F1"), (2, 7, "F2")])

    def test_email_prompt_kind_cannot_change_while_used(self):
        url = reverse("admin:pipeline_emailprompt_change", args=[self.first_a.pk])
        response = self.client.post(url, {"name": "First A", "kind": "follow_up", "subject": "Re: {{previous_subject}}",
                                          "body": "Hi {{first_name|there}}"})
        self.assertContains(response, "Campaigns use it for their initial email")
        page = self.client.get(url).content.decode()
        self.assertIn("initial email", page)  # "Used by" lists the campaign

    def test_email_list_filters_and_dashboard(self):
        first = self.sent_email()
        self.write()
        url = reverse("admin:pipeline_email_changelist")
        self.assertEqual(self.client.get(url, {"sequence_step": "2"}).context["cl"].result_count, 1)
        self.assertEqual(self.client.get(url, {"email_prompt__id__exact": self.f1.pk}).context["cl"].result_count, 1)
        page = self.client.get(reverse("admin:pipeline_email_change", args=[self.followup_of(first).pk])).content.decode()
        self.assertIn("Follow-up 1", page)
        self.sent_email()
        dashboard = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("1 follow-up(s) can be written for review", dashboard)
        self.assertIn("Results by step and initial-email template", dashboard)

    @override_settings(AUTH_TOKEN="fu-key")
    def test_api(self):
        api = APIClient()
        api.credentials(HTTP_AUTH="fu-key")
        created = api.post("/api/follow-ups/", {"campaign": self.campaign.pk, "sequence": 3, "days_after_previous": 14,
                                                "email_prompt": self.f2.pk}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        wrong_kind = api.post("/api/follow-ups/", {"campaign": self.campaign.pk, "sequence": 4,
                                                   "days_after_previous": 1, "email_prompt": self.first_a.pk},
                              format="json")
        self.assertEqual(wrong_kind.status_code, 400)
        campaign = api.get(f"/api/campaigns/{self.campaign.pk}/").json()
        self.assertEqual([f["sequence"] for f in campaign["follow_ups"]], [1, 2, 3])
        self.assertEqual(sorted(campaign["initial_email_prompts"]), sorted([self.first_a.pk, self.first_b.pk]))
        first = self.sent_email()
        response = api.post(f"/api/emails/{first.pk}/mark-replied/")
        self.assertEqual(response.json()["email"]["status"], "replied")
        other = self.sent_email()
        api.post(f"/api/businesses/{other.business_id}/mark-replied/")
        self.assertEqual(Email.objects.get(pk=other.pk).status, "replied")
        third = self.sent_email()
        api.post(f"/api/prospects/{third.prospect_id}/mark-replied/")
        self.assertEqual(Email.objects.get(pk=third.pk).status, "replied")
        nothing_sent = make_email(campaign=self.campaign)
        self.assertEqual(api.post(f"/api/prospects/{nothing_sent.prospect_id}/mark-replied/").status_code, 409)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            run = api.post("/api/runs/", {"command": "generate_followups", "options": {"campaign_id": self.campaign.pk}},
                           format="json")
        self.assertEqual(run.status_code, 202, run.content)
        self.assertEqual(PipelineRun.objects.get().arguments, ["--campaign-id", str(self.campaign.pk)])
