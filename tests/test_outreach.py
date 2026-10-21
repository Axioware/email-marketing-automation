"""Sending from this machine: send times, the Outbox queue, the SMTP sender, the lead pipeline and the review queues."""
import os
import random
import smtplib
from datetime import datetime, time as dt_time, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from pipeline import review
from pipeline.models import (
    BusinessWebsiteProfile,
    DiscoveryCampaign,
    Email,
    FollowUp,
    PipelineRun,
    PipelineStage,
    Prospect,
    SendingSettings,
    StageChange,
)
from pipeline.services import outreach, sending, stages, stakeholders
from tests.support import make_business, make_campaign, make_email, make_email_prompt, review_env

KARACHI = ZoneInfo("Asia/Karachi")
SMTP = {"EMAIL_HOST": "smtp.test", "SENDER_EMAIL": "ali@axioware.tech", "SENDER_NAME": "Ali Khan",
        "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"}


@override_settings(**SMTP)
class OutreachCase(TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, review_env())
        env.start()
        self.addCleanup(env.stop)
        self.initial = make_email_prompt("Initial A", "INITIAL A")
        self.follow = make_email_prompt("Follow 1", "FOLLOW 1", kind="follow_up")
        self.campaign = make_campaign(name="Clinics", initial_prompts=[self.initial])  # default Settings: 120 s + 180-300 s, 40 a day
        self.sequence = self.campaign.email_sequences.get()  # "Initial A": the initial email, then Follow 1
        FollowUp.objects.create(email_sequence=self.sequence, sequence=1, days_after_previous=3, email_prompt=self.follow)
        self.user = get_user_model().objects.create_superuser("q", "q@example.org", "pw")
        self.client.force_login(self.user)

    def email(self, status="in_review", **fields):
        email = make_email(status, campaign=self.campaign, email_sequence=self.sequence, **fields)
        Email.objects.filter(pk=email.pk).update(email_prompt=self.initial)
        email.refresh_from_db()
        return email

    def queued(self, minutes_ago=1, **fields):
        """An approved email queued with a send time that has come."""
        email = self.email()
        review.approve(email)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            review.queue([email])
        Email.objects.filter(pk=email.pk).update(scheduled_at=outreach.now() - timedelta(minutes=minutes_ago), **fields)
        email.refresh_from_db()
        return email

    def send(self, **options):
        return sending.run({"once": True, **options})

    def stage(self, email):
        return Prospect.objects.get(pk=email.prospect_id).stage


class ScheduleTests(OutreachCase):
    def test_gap_between_sends(self):
        rng = random.Random(1)
        start = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        first = self.email("queued")
        Email.objects.filter(pk=first.pk).update(scheduled_at=start)
        slot = outreach.next_slot(self.campaign, start - timedelta(hours=1), rng)
        gap = (slot - start).total_seconds()
        self.assertTrue(120 + 180 <= gap <= 120 + 300, gap)
        self.assertEqual(outreach.next_slot(self.campaign, start + timedelta(hours=5), rng),
                         start + timedelta(hours=5))  # far later than the gap: the earliest time wins

    def test_send_window_and_daily_cap(self):
        DiscoveryCampaign.objects.filter(pk=self.campaign.pk).update(send_window_start=dt_time(9), send_window_end=dt_time(18))
        SendingSettings.objects.update_or_create(pk=1, defaults={"daily_cap": 2})
        self.campaign.refresh_from_db()
        night = datetime(2026, 10, 20, 21, 0, tzinfo=KARACHI)
        self.assertEqual(outreach.next_slot(self.campaign, night), datetime(2026, 10, 21, 9, 0, tzinfo=KARACHI))
        morning = datetime(2026, 10, 21, 7, 0, tzinfo=KARACHI)
        self.assertEqual(outreach.next_slot(self.campaign, morning), datetime(2026, 10, 21, 9, 0, tzinfo=KARACHI))
        for minute in (0, 10):
            email = self.email("queued")
            Email.objects.filter(pk=email.pk).update(scheduled_at=datetime(2026, 10, 21, 9, minute, tzinfo=KARACHI))
        full = outreach.next_slot(self.campaign, datetime(2026, 10, 21, 9, 30, tzinfo=KARACHI))
        self.assertEqual(full, datetime(2026, 10, 22, 9, 0, tzinfo=KARACHI))  # cap of 2 reached that day

    def test_cap_and_gaps_count_every_campaign(self):
        """One mailbox sends for all campaigns: another campaign's emails take the gap and the daily cap too."""
        SendingSettings.objects.update_or_create(pk=1, defaults={"daily_cap": 1, "gap_fixed_seconds": 600,
                                                                 "gap_random_min_seconds": 0,
                                                                 "gap_random_max_seconds": 0})
        other = make_campaign(name="Other", initial_prompts=[self.initial])
        theirs = make_email("queued", campaign=other)
        start = datetime(2026, 10, 20, 10, 0, tzinfo=KARACHI)
        Email.objects.filter(pk=theirs.pk).update(scheduled_at=start)
        self.assertEqual(outreach.next_slot(self.campaign, start), datetime(2026, 10, 21, 0, 0, tzinfo=KARACHI))
        SendingSettings.objects.filter(pk=1).update(daily_cap=40)
        self.assertEqual(outreach.next_slot(self.campaign, start), start + timedelta(seconds=600))

    def test_approve_gives_a_send_time_and_moves_the_lead(self):
        email = self.email()
        message = review.approve(email)
        self.assertIn("Approved for", message)
        self.assertIn("Queue it in the Outbox", message)
        email.refresh_from_db()
        self.assertEqual(email.status, "approved")
        self.assertIsNotNone(email.scheduled_at)
        self.assertEqual(self.stage(email), PipelineStage.READY_TO_SEND)
        self.assertEqual(len(mail.outbox), 0)  # approving never sends

    def test_follow_up_is_not_due_before_its_days(self):
        first = self.email("sent")
        sent_at = outreach.now() - timedelta(days=1)
        Email.objects.filter(pk=first.pk).update(sent_at=sent_at)
        Prospect.objects.filter(pk=first.prospect_id).update(outreach_status="contacted")
        follow_up = Email.objects.create(prospect=first.prospect, business=first.business, contact=first.contact,
                                         sequence_step=2, recipient=first.recipient, subject="Follow", body_text="b",
                                         body_html="<html><body><p>b</p></body></html>", tracking_token="t" * 40,
                                         email_prompt=self.follow)
        review.approve(follow_up)
        follow_up.refresh_from_db()
        self.assertGreaterEqual(follow_up.scheduled_at, sent_at + timedelta(days=3))


class QueueTests(OutreachCase):
    def test_queue_starts_the_sender_once(self):
        first, second = self.email(), self.email()
        review.approve(first)
        review.approve(second)
        with mock.patch("pipeline.jobs.launch") as launch, self.captureOnCommitCallbacks(execute=True):
            message = review.queue([first, second])
        self.assertIn("2 email(s) queued", message)
        self.assertIn("The sender started", message)
        self.assertEqual(Email.objects.filter(status="queued").count(), 2)
        run = PipelineRun.objects.get(command="send_emails")
        launch.assert_called_once_with(run.pk)
        with mock.patch("pipeline.jobs.launch") as launch, mock.patch("pipeline.jobs.process_alive", return_value=True):
            PipelineRun.objects.filter(pk=run.pk).update(status="running", pid=12345)
            third = self.email()
            review.approve(third)
            self.assertIn("The sender is running", review.queue([third]))
        launch.assert_not_called()  # one sender at a time

    def test_a_passed_time_is_kept_and_unqueue_keeps_it_too(self):
        email = self.email()
        review.approve(email)
        overdue = outreach.now() - timedelta(hours=2)
        Email.objects.filter(pk=email.pk).update(scheduled_at=overdue)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            review.queue([email])
        email.refresh_from_db()
        self.assertEqual((email.status, email.scheduled_at), ("queued", overdue))  # due: it goes right away
        self.assertIn("1 email(s) taken out", review.unqueue([email]))
        email.refresh_from_db()
        self.assertEqual((email.status, email.scheduled_at), ("approved", overdue))

    def test_after_a_crash_queueing_the_rest_again_sends_them_right_away(self):
        """20 queued, 5 sent, the app crashed (one mid-send), then the rest are queued again: the overdue ones go
        straight out, the half-sent one is left for a person to check."""
        SendingSettings.objects.update_or_create(pk=1, defaults={"daily_cap": 40})
        emails = []
        for _ in range(20):
            email = self.email()
            review.approve(email)
            emails.append(Email.objects.get(pk=email.pk))
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            self.assertIn("20 email(s) queued", review.queue(emails))
        start = outreach.now() - timedelta(hours=3)
        for minute, email in enumerate(emails):  # the times the crash made them miss
            Email.objects.filter(pk=email.pk).update(scheduled_at=start + timedelta(minutes=minute))
        with mock.patch("pipeline.services.outreach.next_queued", side_effect=list(
                Email.objects.filter(pk__in=[e.pk for e in emails[:5]]).order_by("scheduled_at")) + [None]):
            self.send()  # the first 5 went out before the crash
        Email.objects.filter(pk=emails[5].pk).update(status="sending")  # the crash hit mid-send
        PipelineRun.objects.all().delete()  # its run died with the app
        self.assertEqual(Email.objects.filter(status="sent").count(), 5)
        self.assertEqual(Email.objects.filter(status="queued").count(), 14)

        rest = list(Email.objects.filter(pk__in=[e.pk for e in emails[5:]]))
        with mock.patch("pipeline.jobs.launch") as launch, self.captureOnCommitCallbacks(execute=True):
            message = review.queue(rest)
        self.assertIn("0 email(s) queued. 15 were already queued.", message)
        self.assertIn("The sender started", message)
        launch.assert_called_once()
        self.assertEqual(self.send(), 0)  # the sender: everything overdue goes now
        self.assertEqual(Email.objects.filter(status="sent").count(), 19)
        half_sent = Email.objects.get(pk=emails[5].pk)
        self.assertEqual(half_sent.status, "failed")
        self.assertIn("check your Sent folder", half_sent.send_error)
        self.assertEqual(len(mail.outbox), 19)

    def test_what_cannot_be_queued_is_skipped_with_the_reason(self):
        in_review, stopped = self.email(), self.email()
        review.approve(stopped)
        outreach.stop_prospect(stopped.prospect, "replied")
        message = review.queue([in_review, Email.objects.get(pk=stopped.pk)])
        self.assertIn("0 email(s) queued", message)
        self.assertIn(f"#{in_review.pk} (it is in review)", message)
        self.assertEqual(PipelineRun.objects.count(), 0)

    @override_settings(EMAIL_HOST="")
    def test_without_smtp_settings_emails_wait_in_the_queue(self):
        email = self.email()
        review.approve(email)
        message = review.queue([email])
        self.assertIn("Sending is not set up (SMTP_HOST in .env)", message)
        self.assertEqual(Email.objects.get(pk=email.pk).status, "queued")
        self.assertFalse(PipelineRun.objects.exists())
        with self.assertRaisesMessage(CommandError, "Sending is not set up"):
            self.send()

    def test_editing_or_rejecting_takes_it_out_of_the_queue(self):
        email = self.queued()
        review.save_edit(email, "New subject", "Hi Ali,\n\nNew.")
        email.refresh_from_db()
        self.assertEqual((email.status, email.scheduled_at, email.queued_at), ("in_review", None, None))
        other = self.queued()
        review.reject(other, "no")
        self.assertEqual(Email.objects.get(pk=other.pk).status, "rejected")

    def test_outbox_page(self):
        due = self.queued()
        later = self.email()
        review.approve(later)
        url = reverse("admin:pipeline_outboxemail_changelist")
        page = self.client.get(url)
        self.assertEqual(set(page.context["cl"].result_list), {Email.objects.get(pk=due.pk), Email.objects.get(pk=later.pk)})
        self.assertContains(page, "Queue for sending")
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"action": "queue_selected", "_selected_action": [later.pk]}, follow=True)
        self.assertContains(response, "1 email(s) queued")
        self.assertEqual(Email.objects.get(pk=later.pk).status, "queued")
        PipelineRun.objects.all().delete()
        page = self.client.get(url)
        self.assertContains(page, "are queued but the sender is not running")
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:pipeline_outboxemail_start"))
        self.assertTrue(PipelineRun.objects.filter(command="send_emails").exists())
        sent = self.email("sent")
        self.assertNotIn(Email.objects.get(pk=sent.pk), self.client.get(url).context["cl"].result_list)

    def test_queue_on_the_email_page_and_api(self):
        email = self.email()
        review.approve(email)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:pipeline_email_queue", args=[email.pk]))
        self.assertEqual(Email.objects.get(pk=email.pk).status, "queued")
        self.client.post(reverse("admin:pipeline_email_unqueue", args=[email.pk]))
        self.assertEqual(Email.objects.get(pk=email.pk).status, "approved")
        with override_settings(AUTH_TOKEN="k" * 20):
            api = APIClient()
            api.credentials(HTTP_AUTH="k" * 20)
            with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
                response = api.post("/api/emails/queue/", {"ids": [email.pk]}, format="json")
            self.assertEqual(response.json()["queued"], 1)
            self.assertIn("taken out", api.post("/api/emails/unqueue/", {"ids": [email.pk]}, format="json").json()["detail"])


class SendingTests(OutreachCase):
    def test_sends_due_emails_and_moves_the_lead_on(self):
        email = self.queued()
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual((message.to, message.from_email, message.subject), ([email.recipient], "Ali Khan <ali@axioware.tech>",
                                                                             email.subject))
        self.assertIn("unsubscribe", message.body)  # the footer
        self.assertEqual(message.alternatives[0][1], "text/html")
        self.assertEqual(message.extra_headers["List-Unsubscribe"], "<mailto:ali@axioware.tech?subject=unsubscribe>")
        email.refresh_from_db()
        self.assertEqual((email.status, email.sent_from), ("sent", "ali@axioware.tech"))
        self.assertEqual(email.message_id, message.extra_headers["Message-ID"])
        prospect = Prospect.objects.get(pk=email.prospect_id)
        self.assertEqual((prospect.outreach_status, prospect.stage), ("contacted", PipelineStage.INITIAL_EMAIL))
        follow_up = Email.objects.get(prospect=prospect, sequence_step=2)  # written for review right away
        self.assertEqual(follow_up.status, "in_review")
        self.assertEqual(follow_up.subject, f"Re: {email.subject}")

    def test_a_follow_up_goes_in_the_same_thread(self):
        first = self.queued()
        self.send()
        follow_up = Email.objects.get(prospect_id=first.prospect_id, sequence_step=2)
        Email.objects.filter(pk=first.pk).update(sent_at=outreach.now() - timedelta(days=4))
        review.approve(follow_up)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            review.queue([follow_up])
        Email.objects.filter(pk=follow_up.pk).update(scheduled_at=outreach.now())
        self.send()
        reply = mail.outbox[-1]
        first_id = Email.objects.get(pk=first.pk).message_id
        self.assertEqual((reply.extra_headers["In-Reply-To"], reply.extra_headers["References"]), (first_id, first_id))
        self.assertEqual(self.stage(first), PipelineStage.FOLLOW_UP)
        self.assertFalse(Email.objects.filter(prospect_id=first.prospect_id, sequence_step=3).exists())  # last step

    def test_later_emails_wait_for_their_time(self):
        later = self.queued(minutes_ago=-60)  # due in an hour
        self.assertEqual(self.send(), 0)
        self.assertEqual((len(mail.outbox), Email.objects.get(pk=later.pk).status), (0, "queued"))
        with mock.patch("pipeline.services.sending.time.sleep") as sleep:
            Email.objects.filter(pk=later.pk).update(scheduled_at=outreach.now() + timedelta(seconds=5))

            def arrive(seconds):  # time passes while the sender sleeps
                Email.objects.filter(pk=later.pk).update(scheduled_at=outreach.now() - timedelta(seconds=1))

            sleep.side_effect = arrive
            self.assertEqual(sending.run({"once": False}), 0)
        self.assertEqual(Email.objects.get(pk=later.pk).status, "sent")
        sleep.assert_called_once()

    def test_a_failed_send_is_recorded_and_can_be_queued_again(self):
        email = self.queued()
        with mock.patch("django.core.mail.backends.locmem.EmailBackend.send_messages",
                        side_effect=smtplib.SMTPRecipientsRefused({email.recipient: (550, b"no such user")})):
            self.assertEqual(self.send(), 1)
        email.refresh_from_db()
        self.assertEqual(email.status, "failed")
        self.assertIn("SMTPRecipientsRefused", email.send_error)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            review.queue([email])
        self.assertEqual(Email.objects.get(pk=email.pk).status, "queued")

    def test_a_refused_login_stops_the_sender_and_keeps_the_email_queued(self):
        email = self.queued()
        with mock.patch("django.core.mail.backends.locmem.EmailBackend.send_messages",
                        side_effect=smtplib.SMTPAuthenticationError(535, b"bad password")):
            with self.assertRaisesMessage(CommandError, "refused the login"):
                self.send()
        self.assertEqual(Email.objects.get(pk=email.pk).status, "queued")

    def test_a_half_sent_email_becomes_failed_and_stopped_leads_get_nothing(self):
        half = self.queued()
        Email.objects.filter(pk=half.pk).update(status="sending")
        stopped = self.queued()
        Prospect.objects.filter(pk=stopped.prospect_id).update(do_not_contact=True)
        self.send()
        self.assertIn("check your Sent folder", Email.objects.get(pk=half.pk).send_error)
        self.assertEqual(Email.objects.get(pk=half.pk).status, "failed")
        self.assertEqual(Email.objects.get(pk=stopped.pk).status, "failed")
        self.assertIn("do-not-contact", Email.objects.get(pk=stopped.pk).send_error)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_command_is_a_pipeline_run(self):
        from pipeline import jobs

        self.assertIn("send_emails", jobs.COMMANDS)
        self.assertEqual(jobs.build_arguments("send_emails", {"once": True}), ["--once"])


class StageTests(OutreachCase):
    def test_replied_stops_the_sequence_and_follow_up_resumes_it(self):
        first = self.queued()
        self.send()
        prospect = Prospect.objects.get(pk=first.prospect_id)
        follow_up = Email.objects.get(prospect=prospect, sequence_step=2)
        message = stages.move(prospect, PipelineStage.REPLIED, self.user)
        self.assertIn("its latest email is marked replied", message)
        self.assertIn("1 unsent email(s) cancelled", message)
        self.assertEqual((Email.objects.get(pk=first.pk).status, Email.objects.get(pk=follow_up.pk).status),
                         ("replied", "rejected"))
        prospect.refresh_from_db()
        self.assertEqual((prospect.stage, prospect.sequence_stop_reason), (PipelineStage.REPLIED, "replied"))
        message = stages.move(prospect, PipelineStage.FOLLOW_UP, self.user)  # it was an automatic reply
        self.assertIn("its sequence is resumed", message)
        prospect.refresh_from_db()
        self.assertIsNone(prospect.sequence_stopped_at)
        self.assertEqual((Email.objects.get(pk=first.pk).status, Email.objects.get(pk=follow_up.pk).status),
                         ("sent", "in_review"))
        history = list(StageChange.objects.filter(prospect=prospect).order_by("id").values_list("to_stage", "changed_by"))
        self.assertEqual(history, [(PipelineStage.READY_TO_SEND, None), (PipelineStage.INITIAL_EMAIL, None),
                                   (PipelineStage.REPLIED, self.user.pk), (PipelineStage.FOLLOW_UP, self.user.pk)])

    def test_sending_never_moves_a_lead_out_of_a_stage_a_person_chose(self):
        email = self.queued()
        prospect = Prospect.objects.get(pk=email.prospect_id)
        stages.move(prospect, PipelineStage.DEMO_BOOKED, self.user)  # booked before the email went: it is cancelled
        self.assertEqual(Email.objects.get(pk=email.pk).status, "rejected")
        self.assertFalse(stages.advance(prospect, PipelineStage.INITIAL_EMAIL))
        self.assertEqual(Prospect.objects.get(pk=prospect.pk).stage, PipelineStage.DEMO_BOOKED)

    def test_quota_exhausted_after_the_reply_window(self):
        first = self.queued()
        self.send()
        Email.objects.filter(pk=first.pk).update(sent_at=outreach.now() - timedelta(days=8))
        follow_up = Email.objects.get(prospect_id=first.prospect_id, sequence_step=2)
        Email.objects.filter(pk=follow_up.pk).update(status="sent", sent_at=outreach.now() - timedelta(days=2))
        self.assertEqual(stages.expire_finished(), 0)  # within the 3-day reply window
        Email.objects.filter(pk=follow_up.pk).update(sent_at=outreach.now() - timedelta(days=4))
        self.assertEqual(stages.expire_finished(), 1)
        self.assertEqual(self.stage(first), PipelineStage.QUOTA_EXHAUSTED)
        other = self.queued()
        self.send()
        Email.objects.filter(pk=other.pk).update(sent_at=outreach.now() - timedelta(days=10))
        self.assertEqual(stages.expire_finished(), 0)  # its follow-up is still in review: not finished

    def test_board_page_and_moves(self):
        ready = self.email()
        Email.objects.filter(pk=ready.pk).delete()  # a ready prospect with no email yet: New Lead
        sent = self.queued()
        self.send()
        page = self.client.get(reverse("admin:pipeline_board"))
        columns = {column["value"]: column for column in page.context["columns"]}
        self.assertIn(Prospect.objects.get(pk=ready.prospect_id), columns["new_lead"]["leads"])
        self.assertIn(Prospect.objects.get(pk=sent.prospect_id), columns["initial_email"]["leads"])
        self.assertContains(page, "Demo Booked")
        response = self.client.post(reverse("admin:pipeline_board_move", args=[sent.prospect_id]),
                                    {"stage": "won", "next": reverse("admin:pipeline_board")}, follow=True)
        self.assertContains(response, "Moved to Won")
        self.assertEqual(self.stage(sent), PipelineStage.WON)
        filtered = self.client.get(reverse("admin:pipeline_board"), {"q": sent.recipient})
        self.assertEqual(sum(column["total"] for column in filtered.context["columns"]), 1)
        sidebar = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn(reverse("admin:pipeline_board"), sidebar)
        self.assertIn("Quota Exhausted", sidebar)  # the dashboard's pipeline strip

    @override_settings(AUTH_TOKEN="k" * 20)
    def test_api(self):
        email = self.queued()
        self.send()
        api = APIClient()
        api.credentials(HTTP_AUTH="k" * 20)
        response = api.post(f"/api/prospects/{email.prospect_id}/move-stage/", {"stage": "replied"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["prospect"]["pipeline_stage"], "replied")
        self.assertEqual(api.post(f"/api/prospects/{email.prospect_id}/move-stage/", {"stage": "nowhere"},
                                  format="json").status_code, 400)
        board = api.get("/api/pipeline/").json()["stages"]
        self.assertEqual({row["stage"]: row["leads"] for row in board}["replied"], 1)

    def test_mark_replied_moves_the_lead(self):
        email = self.queued()
        self.send()
        review.mark_replied(Email.objects.get(pk=email.pk))
        self.assertEqual(self.stage(email), PipelineStage.REPLIED)


class ReviewQueueTests(OutreachCase):
    def test_businesses_below_the_minimum_wait_for_a_person(self):
        DiscoveryCampaign.objects.filter(pk=self.campaign.pk).update(min_score=60)
        high = make_business(self.campaign, "High", score=80, website_url="https://high.pk")
        low = make_business(self.campaign, "Low", score=40, website_url="https://low.pk")
        failed = make_business(self.campaign, "Failed", website_url="https://failed.pk")
        BusinessWebsiteProfile.objects.create(business=failed, status="failed")
        names = lambda rows: {row["name"] for row in rows}  # noqa: E731
        self.assertEqual(names(stakeholders.load_businesses(None, None, None, False)), {"High"})
        page = self.client.get(reverse("admin:pipeline_business_changelist"), {"qualification": "review"})
        self.assertEqual({b.name for b in page.context["cl"].result_list}, {"Low", "Failed"})
        self.client.post(reverse("admin:pipeline_business_changelist"),
                         {"action": "approve_businesses", "_selected_action": [low.pk]})
        self.client.post(reverse("admin:pipeline_business_changelist"),
                         {"action": "reject_businesses", "_selected_action": [high.pk]})
        self.assertEqual(names(stakeholders.load_businesses(None, None, None, False)), {"Low"})
        self.assertEqual(names(stakeholders.load_businesses(30, None, None, False)), {"Low"})  # rejected stays out
        dashboard = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("1 business(es) scored below their campaign", dashboard)

    def test_unverified_prospects_wait_for_a_person(self):
        email = self.email()
        prospect = Prospect.objects.get(pk=email.prospect_id)
        email.delete()
        Prospect.objects.filter(pk=prospect.pk).update(email_status="risky", outreach_status="needs_review")
        url = reverse("admin:pipeline_prospect_changelist")
        self.assertIn("1 prospect address(es) could not be verified",
                      self.client.get(reverse("admin:index")).content.decode())
        self.client.post(url, {"action": "approve_prospects", "_selected_action": [prospect.pk]})
        prospect.refresh_from_db()
        self.assertEqual((prospect.outreach_status, prospect.approved_by_person), ("ready", True))
        from pipeline.services import generation

        self.assertEqual([row["prospect_id"] for row in generation.load_prospects({})], [prospect.pk])
        self.client.post(url, {"action": "reject_prospects", "_selected_action": [prospect.pk]})
        self.assertEqual(Prospect.objects.get(pk=prospect.pk).outreach_status, "rejected")
        Prospect.objects.filter(pk=prospect.pk).update(email_status="undeliverable", outreach_status="needs_review")
        self.client.post(url, {"action": "approve_prospects", "_selected_action": [prospect.pk]})
        self.assertEqual(Prospect.objects.get(pk=prospect.pk).outreach_status, "needs_review")  # never undeliverable

    def test_dashboard(self):
        approved = self.email()
        review.approve(approved)
        page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("1 approved email(s) are not queued", page)
        failed = self.queued()
        Email.objects.filter(pk=failed.pk).update(status="failed", send_error="x")
        self.assertIn("1 email(s) failed to send", self.client.get(reverse("admin:index")).content.decode())
        with override_settings(EMAIL_HOST=""):
            self.assertIn("Sending is not set up", self.client.get(reverse("admin:index")).content.decode())
