"""Email sequences: A/B assignment over a campaign's sequences, each sequence's own flow, when follow-ups are written,
template per step, replies (per prospect), admin and API."""
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from pipeline import review
from pipeline.models import DiscoveryCampaign, Email, EmailPrompt, EmailSequence, FollowUp, PipelineRun, Prospect
from pipeline.services import followups, generation
from tests.support import (BASE, CAMPAIGN_INLINE, add_follow_up, make_campaign, make_email, make_email_prompt,
                           make_sequence, run_command)


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
        add_follow_up(self.campaign, 1, 3, self.f1)  # both sequences (First A, First B): F1 after 3 days, F2 after 7
        add_follow_up(self.campaign, 2, 7, self.f2)
        self.sequence_a = EmailSequence.objects.get(name="First A")
        self.sequence_b = EmailSequence.objects.get(name="First B")

    def sent_email(self, days_ago=1, **email_fields):
        """An initial email sent `days_ago` days ago to a contacted prospect (A/B group: First A)."""
        email = make_email("sent", campaign=self.campaign, **email_fields)
        Email.objects.filter(pk=email.pk).update(sent_at=timezone.now() - timedelta(days=days_ago),
                                                 email_prompt=self.first_a)
        Prospect.objects.filter(pk=email.prospect_id).update(outreach_status="contacted",
                                                             email_sequence=self.sequence_a)
        email.refresh_from_db()
        return email

    def write(self, *args):
        return run_command("generate_followups", *args)

    def followup_of(self, email, step=2):
        return Email.objects.filter(prospect_id=email.prospect_id, sequence_step=step).first()


class AssignmentTests(FollowUpCase):
    def test_prospects_are_split_equally_between_sequences_and_keep_theirs(self):
        for _ in range(4):
            make_email(campaign=self.campaign).delete()  # four ready prospects without an email
        result = run_command("generate_emails")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        groups = sorted(Prospect.objects.filter(business__discovery_campaign=self.campaign)
                        .values_list("email_sequence__name", flat=True))
        self.assertEqual(groups, ["First A", "First A", "First B", "First B"])
        for email in Email.objects.select_related("prospect__email_sequence"):
            self.assertEqual(email.email_prompt_id, email.prospect.email_sequence.initial_template_id)
        self.assertEqual(sorted(email.body_text.split("\n\n")[1].split(" about")[0] for email in Email.objects.all()),
                         ["FIRST A", "FIRST A", "FIRST B", "FIRST B"])  # each filled from its own template
        before = dict(Prospect.objects.values_list("id", "email_sequence"))
        run_command("generate_emails", "--regenerate")
        self.assertEqual(dict(Prospect.objects.values_list("id", "email_sequence")), before)  # never reassigned

    def test_balance_counts_existing_prospects(self):
        self.sent_email()
        self.sent_email()  # two prospects already in First A
        email = make_email(campaign=self.campaign)
        row = generation.prospect_rows(Prospect.objects.filter(pk=email.prospect_id))[0]
        self.assertEqual(generation.assign_sequence(row), self.sequence_b.pk)
        self.assertEqual(Prospect.objects.get(pk=email.prospect_id).email_sequence, self.sequence_b)

    def test_campaign_without_sequences_is_skipped(self):
        campaign = make_campaign(name="Bare", initial_prompts=[])
        make_email(campaign=campaign).delete()
        result = run_command("generate_emails")
        self.assertIn("its campaign has no email sequence", result.stdout)
        self.assertFalse(Email.objects.exists())

    def test_a_sequence_without_an_initial_template_is_reported(self):
        empty = make_sequence(name="Empty")
        campaign = make_campaign(name="Half", sequences=[empty])
        make_email(campaign=campaign).delete()
        self.assertIn("its sequence Empty has no initial-email template", run_command("generate_emails").stdout)
        self.assertFalse(Email.objects.exists())


class SequenceFlowTests(FollowUpCase):
    def test_each_sequence_has_its_own_flow(self):
        """Sequence A: initial > F1 (3 days). Sequence B: initial > F2 (2 days) > F1 (5 days) > F2 (9 days)."""
        flow_a = make_sequence(self.first_a, "Flow A", [(3, self.f1)])
        flow_b = make_sequence(self.first_b, "Flow B", [(2, self.f2), (5, self.f1), (9, self.f2)])
        campaign = make_campaign(name="Flows", sequences=[flow_a, flow_b])
        for _ in range(2):
            make_email(campaign=campaign).delete()
        self.assertEqual(run_command("generate_emails").returncode, 0)
        firsts = {email.prospect.email_sequence.name: email
                  for email in Email.objects.filter(business__discovery_campaign=campaign).select_related("prospect__email_sequence")}
        self.assertEqual(set(firsts), {"Flow A", "Flow B"})  # one prospect in each
        self.assertIn("FIRST A", firsts["Flow A"].body_text)
        self.assertIn("FIRST B", firsts["Flow B"].body_text)

        def send(email, days_ago):
            Email.objects.filter(pk=email.pk).update(status="sent", sent_at=timezone.now() - timedelta(days=days_ago))
            Prospect.objects.filter(pk=email.prospect_id).update(outreach_status="contacted")

        for email in firsts.values():
            send(email, 1)
        self.write()
        a1, b1 = self.followup_of(firsts["Flow A"]), self.followup_of(firsts["Flow B"])
        self.assertEqual((a1.email_prompt, b1.email_prompt), (self.f1, self.f2))  # follow-up 1 differs per sequence
        send(a1, 1)
        send(b1, 1)
        self.write()
        self.assertIsNone(self.followup_of(firsts["Flow A"], step=3))  # Flow A ends after one follow-up
        b2 = self.followup_of(firsts["Flow B"], step=3)
        self.assertEqual(b2.email_prompt, self.f1)
        send(b2, 1)
        self.write()
        self.assertEqual(self.followup_of(firsts["Flow B"], step=4).email_prompt, self.f2)
        from pipeline.services import outreach

        self.assertEqual(outreach.earliest_send(self.followup_of(firsts["Flow B"], step=4)),
                         Email.objects.get(pk=b2.pk).sent_at + timedelta(days=9))  # its own step's days
        self.assertIsNone(outreach.sequence_state(firsts["Flow A"].prospect, 2)["count"])  # A: nothing after F1
        self.assertEqual(outreach.sequence_state(firsts["Flow B"].prospect, 2)["count"], 2)  # B: follow-up 2 next


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
                                            email_status="deliverable", outreach_status="contacted",
                                            email_sequence=self.sequence_a)
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
    def test_sequence_page_and_missing_template_warning(self):
        FollowUp.objects.create(email_sequence=self.sequence_a, sequence=3, days_after_previous=10)
        page = self.client.get(reverse("admin:pipeline_emailsequence_change", args=[self.sequence_a.pk]))
        content = page.content.decode()
        self.assertIn("follow_ups-0-email_prompt", content)
        self.assertIn("Follow-up 3 of sequence First A has no template", content)
        self.assertIn("Clinics", content)  # used by
        options = page.context["inline_admin_formsets"][0].formset.forms[0].fields["email_prompt"].queryset
        self.assertEqual(set(options), {self.f1, self.f2})  # follow-up templates only
        choices = set(page.context["adminform"].form.fields["initial_template"].queryset)
        self.assertTrue({self.first_a, self.first_b} <= choices)
        self.assertFalse({self.f1, self.f2} & choices)  # initial templates only
        campaign_page = self.client.get(reverse("admin:pipeline_discoverycampaign_change", args=[self.campaign.pk]))
        self.assertContains(campaign_page, "Follow-up 3 of sequence First A has no template")
        response = self.client.post(reverse("admin:pipeline_emailsequence_add"), {
            "name": "Short", "initial_template": self.first_a.pk,
            "follow_ups-TOTAL_FORMS": "2", "follow_ups-INITIAL_FORMS": "0", "follow_ups-MIN_NUM_FORMS": "0",
            "follow_ups-MAX_NUM_FORMS": "1000",
            "follow_ups-0-sequence": "1", "follow_ups-0-days_after_previous": "3", "follow_ups-0-email_prompt": self.f2.pk,
            "follow_ups-1-sequence": "2", "follow_ups-1-days_after_previous": "7", "follow_ups-1-email_prompt": self.f1.pk})
        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assertEqual(list(FollowUp.objects.filter(email_sequence__name="Short")
                              .values_list("sequence", "days_after_previous", "email_prompt__name")),
                         [(1, 3, "F2"), (2, 7, "F1")])
        created = self.client.post(reverse("admin:pipeline_discoverycampaign_add"), {
            "name": "Two flows", "email_sequences": [self.sequence_a.pk, EmailSequence.objects.get(name="Short").pk]})
        self.assertEqual(created.status_code, 302, created.content[:500])
        self.assertEqual(set(DiscoveryCampaign.objects.get(name="Two flows").email_sequences.values_list("name", flat=True)),
                         {"First A", "Short"})
        listing = self.client.get(reverse("admin:pipeline_emailsequence_changelist"))
        self.assertContains(listing, "Short")

    def test_email_prompt_kind_cannot_change_while_used(self):
        url = reverse("admin:pipeline_emailprompt_change", args=[self.first_a.pk])
        response = self.client.post(url, {"name": "First A", "kind": "follow_up", "subject": "Re: {{previous_subject}}",
                                          "body": "Hi {{first_name|there}}"})
        self.assertContains(response, "Sequences use it for their initial email")
        page = self.client.get(url).content.decode()
        self.assertIn("initial email", page)  # "Used by" lists the sequence

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
        self.assertIn("Results by email sequence (A/B) and step", dashboard)

    @override_settings(AUTH_TOKEN="fu-key")
    def test_api(self):
        api = APIClient()
        api.credentials(HTTP_AUTH="fu-key")
        created = api.post("/api/follow-ups/", {"email_sequence": self.sequence_a.pk, "sequence": 3,
                                                "days_after_previous": 14, "email_prompt": self.f2.pk}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        wrong_kind = api.post("/api/follow-ups/", {"email_sequence": self.sequence_a.pk, "sequence": 4,
                                                   "days_after_previous": 1, "email_prompt": self.first_a.pk},
                              format="json")
        self.assertEqual(wrong_kind.status_code, 400)
        sequence = api.get(f"/api/email-sequences/{self.sequence_a.pk}/").json()
        self.assertEqual([f["sequence"] for f in sequence["follow_ups"]], [1, 2, 3])
        self.assertEqual((sequence["initial_template"], sequence["campaigns"]), (self.first_a.pk, [self.campaign.pk]))
        new = api.post("/api/email-sequences/", {"name": "API flow", "initial_template": self.first_b.pk}, format="json")
        self.assertEqual(new.status_code, 201, new.content)
        self.assertEqual(api.post("/api/email-sequences/", {"name": "Bad", "initial_template": self.f1.pk},
                                  format="json").status_code, 400)  # a follow-up template cannot start a sequence
        campaign = api.patch(f"/api/campaigns/{self.campaign.pk}/", {"email_sequences": [new.json()["id"]]},
                             format="json").json()
        self.assertEqual(campaign["email_sequences"], [new.json()["id"]])
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
