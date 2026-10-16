"""Sending through GoHighLevel: scheduling, hand-over at approval, contact and field writes, the webhooks,
the review queues, queue mode with the worker, and the Vercel settings. GoHighLevel is a local fake server."""
import json
import os
import random
import subprocess
import sys
import threading
from datetime import datetime, time as dt_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from pipeline import jobs, review
from pipeline.models import (
    SendingSettings,
    Business,
    BusinessWebsiteProfile,
    DiscoveryCampaign,
    Email,
    FollowUp,
    PipelineRun,
    Prospect,
    WorkerHeartbeat,
)
from pipeline.services import ghl, outreach, stakeholders
from tests.support import make_business, make_campaign, make_contact, make_email, make_email_prompt, review_env
from tests.test_generate_emails import FakeLLM

KARACHI = ZoneInfo("Asia/Karachi")
FIELD_IDS = {"contact.email_subject": "fSubject", "contact.email_body": "fBody", "contact.email_due_at": "fDue",
             "contact.followup_count": "fCount", "contact.initial_email_type": "fType"}
SECRET = "hook-secret"


class FakeGhl:
    """The LeadConnector endpoints this app uses: custom fields, contact upsert, contact update and read."""

    def __init__(self):
        self.contacts, self.requests, self.fail_with = {}, [], None
        self.apply_then_fail = False  # a write GHL saves but answers with an error (like a timeout after saving)
        ghl._field_cache.clear()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else None
                fake.requests.append((method, self.path, body, self.headers.get("Authorization"),
                                      self.headers.get("User-Agent")))
                if fake.fail_with:
                    return self.reply(fake.fail_with, {"message": "boom"})
                if method == "GET" and "/customFields" in self.path:
                    return self.reply(200, {"customFields": [{"id": i, "fieldKey": k} for k, i in FIELD_IDS.items()]})
                if method == "POST" and self.path == "/contacts/upsert":
                    existing = next((cid for cid, c in fake.contacts.items() if c["email"] == body["email"]), None)
                    cid = existing or f"c{len(fake.contacts) + 1}"
                    fake.contacts.setdefault(cid, {"email": body["email"], "fields": {}, "contact": body})
                    return self.reply(201, {"contact": {"id": cid}, "new": existing is None})
                cid = self.path.split("/")[2] if self.path.startswith("/contacts/") else None
                if method == "PUT" and cid in fake.contacts:
                    for field in body.get("customFields", []):
                        fake.contacts[cid]["fields"][field["id"]] = field["field_value"]
                    if fake.apply_then_fail:
                        return self.reply(504, {"message": "gateway timeout"})
                    return self.reply(200, {"succeded": True})
                if method == "GET" and cid in fake.contacts:
                    fields = [{"id": k, "value": v} for k, v in fake.contacts[cid]["fields"].items() if v not in ("", None)]
                    return self.reply(200, {"contact": {"id": cid, "customFields": fields}})
                return self.reply(404, {"message": "not found"})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PUT(self):
                self.handle_any("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def fields(self, contact_id) -> dict:
        names = {"fSubject": "subject", "fBody": "body", "fDue": "due", "fCount": "count", "fType": "type"}
        return {names[k]: v for k, v in self.contacts[contact_id]["fields"].items() if v not in ("", None)}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OutreachCase(TestCase):
    def setUp(self):
        self.ghl = FakeGhl()
        self.addCleanup(self.ghl.close)
        self.llm = FakeLLM()
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(os.environ, {**review_env(self.llm), "GHL_API_KEY": "pit-test", "GHL_LOCATION_ID": "loc1",
                                           "GHL_BASE_URL": self.ghl.url, "GHL_WEBHOOK_SECRET": SECRET})
        env.start()
        self.addCleanup(env.stop)
        self.initial = make_email_prompt("Initial A", "INITIAL A")
        self.follow = make_email_prompt("Follow 1", "FOLLOW 1", kind="follow_up")
        self.campaign = make_campaign(name="Clinics", initial_prompts=[self.initial])  # default Settings: 120 s + 180-300 s, 40 a day
        FollowUp.objects.create(campaign=self.campaign, sequence=1, days_after_previous=3, email_prompt=self.follow)
        self.api = APIClient()

    def email(self, status="in_review", **fields):
        email = make_email(status, campaign=self.campaign, **fields)
        Email.objects.filter(pk=email.pk).update(email_prompt=self.initial)
        email.refresh_from_db()
        return email

    def hook(self, path, body, secret=SECRET):
        return self.api.post(f"/api/ghl/{path}/", body, format="json", HTTP_X_WEBHOOK_SECRET=secret)

    def contact_id(self, email):
        return Prospect.objects.get(pk=email.prospect_id).ghl_contact_id

    def puts(self):
        return [r for r in self.ghl.requests if r[0] == "PUT"]


class ScheduleTests(OutreachCase):
    def test_gap_between_sends(self):
        rng = random.Random(1)
        start = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        first = self.email("scheduled")
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
        slot = outreach.next_slot(self.campaign, night)
        self.assertEqual(slot, datetime(2026, 10, 21, 9, 0, tzinfo=KARACHI))  # next morning
        morning = datetime(2026, 10, 21, 7, 0, tzinfo=KARACHI)
        self.assertEqual(outreach.next_slot(self.campaign, morning), datetime(2026, 10, 21, 9, 0, tzinfo=KARACHI))
        for minute in (0, 10):
            email = self.email("scheduled")
            Email.objects.filter(pk=email.pk).update(scheduled_at=datetime(2026, 10, 21, 9, minute, tzinfo=KARACHI))
        full = outreach.next_slot(self.campaign, datetime(2026, 10, 21, 9, 30, tzinfo=KARACHI))
        self.assertEqual(full, datetime(2026, 10, 22, 9, 0, tzinfo=KARACHI))  # cap of 2 reached that day

    def test_cap_and_gaps_count_every_campaign(self):
        """One mailbox sends for all campaigns: another campaign's emails take the gap and the daily cap too."""
        SendingSettings.objects.update_or_create(pk=1, defaults={"daily_cap": 1, "gap_fixed_seconds": 600,
                                                                 "gap_random_min_seconds": 0,
                                                                 "gap_random_max_seconds": 0})
        other = make_campaign(name="Other", initial_prompts=[self.initial])
        theirs = make_email("scheduled", campaign=other)
        start = datetime(2026, 10, 20, 10, 0, tzinfo=KARACHI)
        Email.objects.filter(pk=theirs.pk).update(scheduled_at=start)
        self.assertEqual(outreach.next_slot(self.campaign, start), datetime(2026, 10, 21, 0, 0, tzinfo=KARACHI))
        SendingSettings.objects.filter(pk=1).update(daily_cap=40)
        self.assertEqual(outreach.next_slot(self.campaign, start), start + timedelta(seconds=600))

    def test_approve_hands_the_email_to_ghl(self):
        email = self.email()
        message = review.approve(email)
        self.assertIn("Approved and handed to GoHighLevel; due", message)
        email.refresh_from_db()
        self.assertEqual(email.status, "scheduled")
        self.assertIsNotNone(email.released_at)
        contact = self.contact_id(email)
        payload = self.ghl.contacts[contact]["contact"]
        self.assertEqual(payload["email"], email.recipient)
        self.assertIn(ghl.OUTREACH_TAG, payload["tags"])
        self.assertIn("campaign-clinics", payload["tags"])
        fields = self.ghl.fields(contact)
        self.assertEqual((fields["subject"], fields["count"], fields["type"]), (email.subject, 0, "Initial A"))
        self.assertEqual(fields["due"], email.scheduled_at.astimezone(KARACHI).replace(microsecond=0).isoformat())
        self.assertIn("<p>", fields["body"])
        self.assertNotIn("<html", fields["body"])
        self.assertIn("unsubscribe", fields["body"])  # the footer is part of the body
        _, _, _, auth, agent = self.ghl.requests[0]
        self.assertEqual((auth, agent), ("Bearer pit-test", ghl.USER_AGENT))

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
        fields = self.ghl.fields(self.contact_id(first))
        self.assertEqual(fields["count"], 1)
        self.assertNotIn("type", fields)  # only initial emails set the type

    def test_reject_or_reopen_takes_it_back_out_of_ghl(self):
        email = self.email()
        review.approve(email)
        email.refresh_from_db()
        review.reopen(email)
        email.refresh_from_db()
        self.assertEqual((email.status, email.scheduled_at), ("in_review", None))
        self.assertEqual(self.ghl.fields(self.contact_id(email)), {"count": 0, "type": "Initial A"})  # nothing to send
        review.approve(email)
        email.refresh_from_db()
        self.ghl.fail_with = 500
        with self.assertRaisesMessage(review.ReviewError, "Could not take it back out of GoHighLevel"):
            review.reject(email, "no")
        self.assertEqual(Email.objects.get(pk=email.pk).status, "scheduled")  # unchanged: GHL could still send it
        self.ghl.fail_with = None
        review.reject(Email.objects.get(pk=email.pk), "no")
        self.assertEqual(Email.objects.get(pk=email.pk).status, "rejected")
        self.assertNotIn("body", self.ghl.fields(self.contact_id(email)))

    def test_editing_a_handed_over_email_takes_it_back(self):
        email = self.email()
        review.approve(email)
        review.save_edit(Email.objects.get(pk=email.pk), "New subject", "Hi Ali,\n\nChanged.")
        email.refresh_from_db()
        self.assertEqual((email.status, email.scheduled_at), ("in_review", None))
        self.assertNotIn("subject", self.ghl.fields(self.contact_id(email)))

    def test_a_failed_hand_over_stays_approved_and_can_be_retried(self):
        self.ghl.fail_with = 500
        email = self.email()
        message = review.approve(email)
        self.assertIn("not handed to GoHighLevel", message)
        email.refresh_from_db()
        self.assertEqual(email.status, "approved")
        self.assertIn("HTTP 500", email.ghl_error)
        self.assertIsNotNone(email.scheduled_at)  # its slot is kept
        self.assertEqual(list(outreach.not_handed_over()), [email])
        self.ghl.fail_with = None
        self.assertIn("Handed to GoHighLevel", review.hand_over(email))
        email.refresh_from_db()
        self.assertEqual((email.status, email.ghl_error), ("scheduled", None))

    def test_a_write_ghl_saved_despite_an_error_is_never_written_twice(self):
        email = self.email()
        self.ghl.apply_then_fail = True  # GHL saves the email but answers with a timeout
        review.approve(email)
        email.refresh_from_db()
        self.assertEqual(email.status, "approved")
        self.assertEqual(len(self.puts()), 1)  # no automatic retry of the write
        self.ghl.apply_then_fail = False
        review.hand_over(email)
        self.assertEqual(len(self.puts()), 1)  # GHL already had it: marked handed over without writing again
        self.assertEqual(Email.objects.get(pk=email.pk).status, "scheduled")

    def test_only_one_email_of_a_prospect_waits_in_ghl(self):
        email = self.email()
        review.approve(email)
        second = Email.objects.create(prospect=email.prospect, business=email.business, contact=email.contact,
                                      sequence_step=2, recipient=email.recipient, subject="F", body_text="b",
                                      body_html="h", tracking_token="v" * 40, status="approved")
        with self.assertRaisesMessage(outreach.OutreachError, "is still waiting in GoHighLevel"):
            outreach.hand_over(second)


class WebhookTests(OutreachCase):
    def handed_over(self):
        email = self.email()
        review.approve(email)
        return Email.objects.get(pk=email.pk)

    def test_ghl_webhook_action_payload(self):
        """The body GHL's Webhook action sends: contact fields at the top, our keys in customData, no headers."""
        email = self.handed_over()
        contact = self.contact_id(email)
        payload = {"contact_id": contact, "first_name": "Hassam", "email": email.recipient, "tags": "",
                   "location": {"id": "N5kPOFObjirSmBMBqugi"}, "workflow": {"id": "w", "name": "Send email"},
                   "triggerData": {}, "contact": {"attributionSource": {}}, "attributionSource": {},
                   "customData": {"email_subject": "", "email_body": "", "initial_email_type": "Initial A",
                                  "email_due_at": "", "followup_count": "0", "secret": "wrong"}}
        self.assertEqual(self.api.post("/api/ghl/email-sent/", payload, format="json").status_code, 401)
        payload["customData"]["secret"] = SECRET
        response = self.api.post("/api/ghl/email-sent/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Email.objects.get(pk=email.pk).status, "sent")
        payload["customData"].pop("secret")
        retried = self.api.post(f"/api/ghl/email-sent/?secret={SECRET}", payload, format="json")
        self.assertEqual(retried.json()["outcome"], "already recorded")
        stop = {"contact_id": contact, "customData": {"reason": "replied", "secret": SECRET}}
        self.assertEqual(self.api.post("/api/ghl/stop-sequence/", stop, format="json").status_code, 200)
        self.assertEqual(Email.objects.get(pk=email.pk).status, "replied")

    def test_email_sent_webhook_writes_the_next_follow_up(self):
        email = self.handed_over()
        contact = self.contact_id(email)
        self.assertEqual(self.hook("email-sent", {"contact_id": contact}, secret="wrong").status_code, 401)
        with mock.patch.dict(os.environ, {"GHL_WEBHOOK_SECRET": ""}):
            self.assertEqual(self.hook("email-sent", {"contact_id": contact}).status_code, 503)
        self.assertEqual(self.hook("email-sent", {}).status_code, 400)
        self.assertEqual(self.hook("email-sent", {"contact_id": "nobody"}).status_code, 404)
        response = self.hook("email-sent", {"contact_id": contact, "followup_count": "0"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["next"], response.json()["follow_up"]),
                         ("follow-up 1", "follow-up 1 written for review"))
        email.refresh_from_db()
        self.assertEqual(email.status, "sent")
        self.assertIsNotNone(email.sent_at)
        self.assertEqual(Prospect.objects.get(pk=email.prospect_id).outreach_status, "contacted")
        self.assertEqual(self.ghl.fields(contact), {"count": 1, "type": "Initial A"})  # waiting for follow-up 1
        follow_up = Email.objects.get(prospect_id=email.prospect_id, sequence_step=2)
        self.assertEqual((follow_up.status, follow_up.email_prompt), ("in_review", self.follow))
        again = self.hook("email-sent", {"contact_id": contact, "followup_count": 0})  # GHL retried
        self.assertEqual(again.json()["outcome"], "already recorded")
        self.assertEqual(Email.objects.filter(prospect_id=email.prospect_id).count(), 2)  # no second follow-up
        self.assertEqual(Email.objects.get(pk=email.pk).sent_at, email.sent_at)

    def test_a_follow_up_the_ai_could_not_write_is_left_for_later(self):
        email = self.handed_over()
        self.llm.mode = "bad"
        response = self.hook("email-sent", {"contact_id": self.contact_id(email), "followup_count": "0"})
        self.assertEqual(response.status_code, 200)  # the email was sent either way
        self.assertIn("follow-up not written", response.json()["follow_up"])
        self.assertEqual(Email.objects.get(pk=email.pk).status, "sent")

    def test_an_email_whose_hand_over_timed_out_is_still_recorded(self):
        email = self.email()
        self.ghl.apply_then_fail = True
        review.approve(email)
        self.ghl.apply_then_fail = False
        response = self.hook("email-sent", {"contact_id": self.contact_id(email), "customData": {}})
        self.assertEqual(response.json()["outcome"], "recorded")
        self.assertEqual(Email.objects.get(pk=email.pk).status, "sent")

    def test_stop_sequence_webhook(self):
        first = self.handed_over()
        contact = self.contact_id(first)
        self.hook("email-sent", {"contact_id": contact, "followup_count": 0})
        pending = Email.objects.get(prospect_id=first.prospect_id, sequence_step=2)  # written by the webhook
        response = self.hook("stop-sequence", {"contact_id": contact, "reason": "replied"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["cancelled"], 1)
        self.assertEqual((Email.objects.get(pk=first.pk).status, Email.objects.get(pk=pending.pk).status),
                         ("replied", "rejected"))
        self.assertEqual(Prospect.objects.get(pk=first.prospect_id).sequence_stop_reason, "replied")
        self.assertEqual(self.ghl.fields(contact), {"type": "Initial A"})  # nothing left to send

    def test_full_sequence_until_exhausted(self):
        email = self.handed_over()
        contact = self.contact_id(email)
        self.hook("email-sent", {"contact_id": contact, "followup_count": 0})
        follow_up = Email.objects.get(prospect_id=email.prospect_id, sequence_step=2)
        review.approve(follow_up)
        self.assertEqual(self.ghl.fields(contact)["count"], 1)
        self.assertEqual(Email.objects.get(pk=follow_up.pk).status, "scheduled")
        response = self.hook("email-sent", {"contact_id": contact, "followup_count": 1})
        self.assertEqual(response.json()["next"], "sequence finished")
        self.assertNotIn("follow_up", response.json())
        self.assertEqual(self.ghl.fields(contact), {"type": "Initial A"})  # Follow-up Count empty: exhausted
        self.assertEqual(Email.objects.get(pk=follow_up.pk).status, "sent")


class ContactTests(OutreachCase):
    def test_contacts_for_ready_prospects_only(self):
        self.email().delete()
        business = make_business(self.campaign, "Unverified clinic")
        contact = make_contact(business, name="Sara Khan")
        unverified = Prospect.objects.create(business=business, contact=contact, email="sara@unverified.org",
                                             email_status="risky", outreach_status="needs_review")
        result = outreach.sync_contacts()
        self.assertEqual(result["created"], 1)
        self.assertIsNone(Prospect.objects.get(pk=unverified.pk).ghl_contact_id)
        Prospect.objects.filter(pk=unverified.pk).update(outreach_status="ready", approved_by_person=True)
        self.assertEqual(outreach.sync_contacts()["created"], 1)
        self.assertTrue(Prospect.objects.get(pk=unverified.pk).ghl_contact_id)
        self.assertEqual(outreach.sync_contacts(), {"created": 0, "matched": 0, "failed": 0})

    def test_one_sequence_per_email_address(self):
        from pipeline.services import generation

        first = self.email()
        first.delete()
        other_business = make_business(self.campaign, "Second branch")
        other_contact = make_contact(other_business, name="Front desk")
        shared = Prospect.objects.create(business=other_business, contact=other_contact,
                                         email=first.recipient.upper(), email_status="deliverable",
                                         outreach_status="ready")
        ready = [p.pk for p in generation.ready_prospects()]
        self.assertEqual(ready, [first.prospect_id])  # the address gets one sequence: the oldest prospect's
        outreach.sync_contacts()
        self.assertIsNone(Prospect.objects.get(pk=shared.pk).ghl_contact_id)
        Prospect.objects.filter(pk=first.prospect_id).update(do_not_contact=True)
        self.assertEqual(list(generation.ready_prospects()), [])  # an opted-out address is out everywhere
        with self.assertRaisesMessage(outreach.OutreachError, "one email address gets one sequence"):
            outreach.link_contact(shared, self.contact_id(first))

    def test_ghl_sync_command_hands_over_and_creates_contacts(self):
        from tests.support import run_command

        self.ghl.fail_with = 500
        email = self.email()
        review.approve(email)
        self.ghl.fail_with = None
        result = run_command("ghl_sync")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Approved emails handed to GoHighLevel: 1", result.stdout)
        self.assertEqual(Email.objects.get(pk=email.pk).status, "scheduled")
        with mock.patch.dict(os.environ, {"GHL_API_KEY": ""}):
            self.assertIn("GoHighLevel is not set up", run_command("ghl_sync").stdout)

    def test_writes_are_never_retried_but_rate_limits_are(self):
        client = ghl.Client()
        self.ghl.fail_with = 500
        with mock.patch("pipeline.services.ghl.time.sleep"), self.assertRaises(ghl.GhlError):
            client.request("PUT", "/contacts/x", {})
        self.assertEqual(len(self.puts()), 1)
        self.ghl.fail_with = 429
        with mock.patch("pipeline.services.ghl.time.sleep"), self.assertRaises(ghl.GhlError):
            client.request("PUT", "/contacts/x", {})
        self.assertEqual(len(self.puts()), 1 + ghl.RETRIES)  # a 429 applied nothing: retried

    def test_missing_custom_fields_are_named(self):
        client = ghl.Client()
        ghl._field_cache.clear()
        with mock.patch.object(ghl.Client, "request", return_value={"customFields": []}):
            with self.assertRaisesMessage(ghl.GhlError, "contact.email_subject"):
                client.field_ids()

    def test_tests_never_reach_the_real_ghl(self):
        with mock.patch.dict(os.environ, {"GHL_BASE_URL": ""}):
            with self.assertRaisesMessage(ghl.GhlError, "Tests never call the real GoHighLevel"):
                ghl.Client()


class ReviewQueueTests(OutreachCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("q", "q@example.org", "pw"))

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

    def test_dashboard_warnings(self):
        email = self.email()
        review.approve(email)
        Email.objects.filter(pk=email.pk).update(scheduled_at=outreach.now() - timedelta(days=2))
        page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("never reported sent", page)
        failed = self.email()
        self.ghl.fail_with = 500
        review.approve(failed)
        self.ghl.fail_with = None
        page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("1 approved email(s) are not in GoHighLevel yet", page)
        with mock.patch.dict(os.environ, {"GHL_API_KEY": "", "GHL_WEBHOOK_SECRET": ""}):
            page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("GoHighLevel is not set up", page)
        self.assertIn("GHL_WEBHOOK_SECRET is not set", page)


class QueueModeTests(TestCase):
    def test_runs_wait_for_the_worker_and_stop_requests_go_through_the_database(self):
        with mock.patch.dict(os.environ, {"RUN_MODE": "queue"}), mock.patch("pipeline.jobs.launch") as launch, \
                self.captureOnCommitCallbacks(execute=True):
            run = jobs.start_run("generate_followups", [])
        launch.assert_not_called()
        PipelineRun.objects.filter(pk=run.pk).update(status="running", pid=2 ** 22 + 1)
        with mock.patch.dict(os.environ, {"RUN_MODE": "queue"}):
            jobs.refresh_stale_runs()  # the pid lives on the worker's machine: left alone
            self.assertEqual(PipelineRun.objects.get(pk=run.pk).status, "running")
            self.assertIn("Stop requested", jobs.cancel_run(PipelineRun.objects.get(pk=run.pk)))
        self.assertTrue(PipelineRun.objects.get(pk=run.pk).cancel_requested)


class WorkerTests(TransactionTestCase):
    def test_worker_runs_queued_runs_and_reports_in(self):
        from django.core.management import call_command

        run = PipelineRun.objects.create(command="generate_followups", arguments=["--limit", "1"])
        db = settings.DATABASES["default"]
        url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        blank = {name: "" for name in ("GHL_API_KEY", "GHL_LOCATION_ID", "GROQ_API_KEY", "GROK_API_KEY",
                                       "OPENAI_API_KEY")}  # the run's process reads .env; empty values win
        with mock.patch.dict(os.environ, {"DATABASE_URL": url, "DJANGO_DEBUG": "1", **blank}), \
                mock.patch("pipeline.services.scraper_api.status", return_value=None):
            call_command("run_worker", "--once", "--poll", "0.1")
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded", run.output)
        self.assertIn("No follow-ups are due", run.output)
        self.assertTrue(WorkerHeartbeat.objects.exists())


class VercelSettingsTests(TestCase):
    def test_settings_on_vercel(self):
        code = ("import os; os.environ['DJANGO_SETTINGS_MODULE']='emailautomation.settings'; import django; "
                "django.setup(); from django.conf import settings as s; "
                "print(s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS, os.environ['RUN_MODE'], "
                "s.DATABASES['default']['CONN_MAX_AGE'], s.DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'], "
                "s.SESSION_COOKIE_SECURE, s.SECURE_PROXY_SSL_HEADER)")
        env = {key: value for key, value in os.environ.items() if key != "RUN_MODE"}
        env.update(VERCEL="1", VERCEL_URL="app-abc.vercel.app", DJANGO_SECRET_KEY="x" * 50, DJANGO_DEBUG="0",
                   DATABASE_URL="postgresql://u:p@pooler.supabase.com:6543/postgres")
        output = subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(output.returncode, 0, output.stderr)
        printed = output.stdout.strip()
        self.assertIn("'app-abc.vercel.app'", printed)
        self.assertIn("'https://app-abc.vercel.app'", printed)
        self.assertIn(" queue 0 True True ('HTTP_X_FORWARDED_PROTO', 'https')", printed)
