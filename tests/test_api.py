"""The REST API: authentication, every resource, the review actions and pipeline runs."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from pipeline.models import BusinessContact, DiscoveryCampaign, Email, PipelineRun, Prospect
from tests.fake_smtp import FakeSmtpServer
from tests.support import BASE, make_business, make_campaign, make_contact, make_email, review_env
from tests.test_generate_emails import FakeLLM


KEY = "test-api-key-0123456789"


@override_settings(AUTH_TOKEN=KEY)
class ApiCase(TestCase):
    def setUp(self):
        self.smtp = FakeSmtpServer()
        self.llm = FakeLLM()
        self.addCleanup(self.smtp.close)
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(os.environ, review_env(self.smtp, self.llm))
        env.start()
        self.addCleanup(env.stop)
        self.user = get_user_model().objects.create_user("staff", "staff@example.org", "pw", is_staff=True)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTH=KEY)
        self.api.force_authenticate(self.user)


class AuthTests(ApiCase):
    PATHS = ("/api/", "/api/emails/", "/api/runs/", "/api/runs/commands/", "/api/stats/", "/api/campaigns/")

    def test_every_endpoint_needs_the_auth_header(self):
        for headers in ({}, {"HTTP_AUTH": "wrong"}, {"HTTP_AUTH": KEY[:-1]}, {"HTTP_AUTHORIZATION": KEY}):
            client = APIClient()
            client.force_authenticate(self.user)  # even a logged-in staff user is refused without the key
            for path in self.PATHS:
                response = client.get(path, **headers)
                self.assertEqual(response.status_code, 401, (path, headers))
                self.assertEqual(response["WWW-Authenticate"], "Auth")
        self.assertEqual(APIClient().post("/api/campaigns/", {"name": "x"}).status_code, 401)
        self.assertFalse(DiscoveryCampaign.objects.exists())

    def test_the_header_alone_is_enough(self):
        client = APIClient()
        client.credentials(HTTP_AUTH=KEY)
        for path in self.PATHS:
            self.assertEqual(client.get(path).status_code, 200, path)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            response = client.post("/api/runs/", {"command": "send_emails"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertIsNone(PipelineRun.objects.get().created_by)

    @override_settings(AUTH_TOKEN="")
    def test_api_is_disabled_without_a_configured_key(self):
        for headers in ({}, {"HTTP_AUTH": ""}):
            response = self.api.get("/api/stats/", **headers)
            self.assertEqual(response.status_code, 401)
            self.assertIn("AUTH_TOKEN", response.json()["detail"])

    def test_user_token_with_the_header_attributes_runs(self):
        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(HTTP_AUTH=KEY, HTTP_AUTHORIZATION=f"Token {token.key}")
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            client.post("/api/runs/", {"command": "send_emails"}, format="json")
        self.assertEqual(PipelineRun.objects.get().created_by, self.user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")  # a user token without the header is refused
        self.assertEqual(client.get("/api/stats/").status_code, 401)

    def test_session_writes_still_need_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        client.login(username="staff", password="pw")
        client.credentials(HTTP_AUTH=KEY)
        self.assertEqual(client.get("/api/emails/").status_code, 200)
        self.assertEqual(client.post("/api/campaigns/", {"name": "x"}).status_code, 403)

    def test_password_token_endpoint_is_gone(self):
        self.assertEqual(self.api.post("/api/auth/token/", {"username": "staff", "password": "pw"}).status_code, 404)

    def test_docs_need_an_admin_login_and_describe_the_header(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get("/api/schema/", HTTP_AUTH=KEY).status_code, (401, 403))
        schema = self.api.get("/api/schema/", {"format": "json"})
        self.assertEqual(schema.status_code, 200)
        data = schema.json()
        self.assertEqual(data["components"]["securitySchemes"]["AuthHeader"],
                         {"type": "apiKey", "in": "header", "name": "Auth", "description": "The AUTH_TOKEN value from .env."})
        self.assertIn("/api/emails/{id}/approve/", data["paths"])
        self.assertEqual(self.api.get("/api/docs/").status_code, 200)


class ResourceTests(ApiCase):
    def test_every_resource_lists_and_shows(self):
        email = make_email("sent")
        for path in ("campaigns", "businesses", "business-sources", "website-profiles", "contacts", "prospects",
                     "emails", "email-opens", "runs"):
            response = self.api.get(f"/api/{path}/")
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("results", response.json(), path)
        for path, pk in (("campaigns", email.business.discovery_campaign_id), ("businesses", email.business_id),
                         ("website-profiles", email.business.website_profile.pk), ("contacts", email.contact_id),
                         ("prospects", email.prospect_id), ("emails", email.pk)):
            self.assertEqual(self.api.get(f"/api/{path}/{pk}/").status_code, 200, path)
        self.assertEqual(self.api.get("/api/router-root-check/").status_code, 404)

    def test_create_campaign_with_lists_or_comma_strings(self):
        response = self.api.post("/api/campaigns/", {"name": "Lahore", "target_country": "Pakistan",
                                                      "target_locations": "Lahore, DHA", "search_terms": ["dentist"]},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.content)
        campaign = DiscoveryCampaign.objects.get()
        self.assertEqual((campaign.target_locations, campaign.search_terms, campaign.status), (["Lahore", "DHA"], ["dentist"], "pending"))
        bad = self.api.post("/api/campaigns/", {"name": "x", "target_country": "A, B"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_filters_search_and_ordering(self):
        campaign = make_campaign()
        good = make_business(campaign, "Smile Dental", score=80, city="Lahore")
        make_business(campaign, "Tooth Co", score=30, city="Karachi")
        names = lambda r: [row["name"] for row in r.json()["results"]]  # noqa: E731
        self.assertEqual(names(self.api.get("/api/businesses/", {"search": "smile"})), ["Smile Dental"])
        self.assertEqual(names(self.api.get("/api/businesses/", {"city": "Karachi"})), ["Tooth Co"])
        self.assertEqual(names(self.api.get("/api/businesses/", {"website_profile__qualification_score__gte": 50})), ["Smile Dental"])
        self.assertEqual(names(self.api.get("/api/businesses/", {"ordering": "-website_profile__qualification_score"}))[0], "Smile Dental")
        self.assertEqual(self.api.get(f"/api/businesses/{good.pk}/").json()["qualification_score"], 80)

    def test_update_contact_and_prospect(self):
        business = make_business(name="B")
        contact = make_contact(business, "a@b.org")
        response = self.api.patch(f"/api/contacts/{contact.pk}/", {"job_title": "Owner"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BusinessContact.objects.get().job_title, "Owner")
        prospect = Prospect.objects.create(business=business, contact=contact, email="a@b.org")
        self.api.patch(f"/api/prospects/{prospect.pk}/", {"do_not_contact": True, "email_verification_provider": "x"}, format="json")
        prospect.refresh_from_db()
        self.assertTrue(prospect.do_not_contact)
        self.assertIsNone(prospect.email_verification_provider)  # read-only

    def test_stats(self):
        make_email("sent")
        make_email("opened")
        data = self.api.get("/api/stats/").json()
        self.assertEqual(data["emails"]["sent"], 1)
        self.assertEqual(data["open_rate"], 50)
        self.assertEqual(data["businesses"], 2)


class EmailApiTests(ApiCase):
    def test_body_is_the_editable_part_and_editing_goes_back_to_review(self):
        email = make_email("approved")
        data = self.api.get(f"/api/emails/{email.pk}/").json()
        self.assertEqual(data["body"], "Hi Ali,\n\nA short note.")
        self.assertTrue(data["prospect_ready"])
        response = self.api.patch(f"/api/emails/{email.pk}/", {"subject": "New  subject", "body": "Hi Ali,\n\nNew."}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        email.refresh_from_db()
        self.assertEqual((email.subject, email.status), ("New subject", "in_review"))
        self.assertIn(f"{BASE}/{email.tracking_token}", email.body_html)
        self.assertEqual(response.json()["body"], "Hi Ali,\n\nNew.")

    def test_read_only_fields_cannot_be_changed(self):
        email = make_email()
        self.api.patch(f"/api/emails/{email.pk}/", {"status": "sent", "recipient": "x@y.org", "open_count": 5}, format="json")
        email.refresh_from_db()
        self.assertEqual((email.status, email.open_count), ("in_review", 0))
        self.assertNotEqual(email.recipient, "x@y.org")

    def test_sent_emails_cannot_be_edited(self):
        email = make_email("sent")
        response = self.api.patch(f"/api/emails/{email.pk}/", {"subject": "Changed"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Email.objects.get().subject, "Hello there")

    def test_emails_are_not_created_through_the_api(self):
        self.assertEqual(self.api.post("/api/emails/", {}).status_code, 405)

    def test_review_actions(self):
        email = make_email()
        response = self.api.post(f"/api/emails/{email.pk}/approve/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"]["status"], "approved")
        self.assertEqual(self.api.post(f"/api/emails/{email.pk}/approve/").status_code, 409)  # not in review any more
        self.api.post(f"/api/emails/{email.pk}/reject/", {"note": "Too long"}, format="json")
        email.refresh_from_db()
        self.assertEqual((email.status, email.review_note), ("rejected", "Too long"))
        self.api.post(f"/api/emails/{email.pk}/reopen/")
        email.refresh_from_db()
        self.assertEqual(email.status, "in_review")

    def test_regenerate(self):
        email = make_email("rejected")
        response = self.api.post(f"/api/emails/{email.pk}/regenerate/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Idea for Clinic", response.json()["email"]["subject"])
        self.assertEqual(response.json()["email"]["tracking_token"], email.tracking_token)

    def test_send_needs_confirmation(self):
        email = make_email("approved")
        self.assertEqual(self.api.post(f"/api/emails/{email.pk}/send/", {}, format="json").status_code, 400)
        self.assertEqual(self.api.post(f"/api/emails/{email.pk}/send/", {"confirm": "x@y.org"}, format="json").status_code, 409)
        self.assertEqual(self.smtp.messages, [])
        response = self.api.post(f"/api/emails/{email.pk}/send/", {"confirm": email.recipient}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["email"]["status"], "sent")
        self.assertEqual(len(self.smtp.messages), 1)

    def test_bulk(self):
        a, b = make_email(), make_email(body="PLACEHOLDER")
        response = self.api.post("/api/emails/bulk/", {"action": "approve", "ids": [a.pk, b.pk, 999999]}, format="json")
        self.assertEqual(response.json(), {"done": 1, "skipped": 2})
        self.assertEqual(self.api.post("/api/emails/bulk/", {"action": "send", "ids": [a.pk]}, format="json").status_code, 400)

    def test_preview_is_sandboxed_html_without_the_tracking_url(self):
        email = make_email()
        response = self.api.get(f"/api/emails/{email.pk}/preview/")
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertNotIn(email.tracking_token, response.content.decode())
        self.assertIn("A short note.", response.content.decode())

    def test_status_filter_and_stats(self):
        make_email("approved")
        make_email()
        self.assertEqual(self.api.get("/api/emails/", {"status": "approved"}).json()["count"], 1)
        stats = self.api.get("/api/emails/stats/").json()
        self.assertEqual((stats["counts"]["approved"], stats["counts"]["in_review"], stats["total"]), (1, 1, 2))


class RunApiTests(ApiCase):
    def start(self, payload):
        with mock.patch("pipeline.jobs.launch") as launch, self.captureOnCommitCallbacks(execute=True):
            response = self.api.post("/api/runs/", payload, format="json")
        return response, launch

    def test_commands_lists_options(self):
        commands = {c["command"]: c for c in self.api.get("/api/runs/commands/").json()}
        self.assertEqual(set(commands), {"fetch_businesses", "research_websites", "find_stakeholders", "verify_emails",
                                         "generate_emails", "send_emails"})
        options = {o["name"]: o for o in commands["verify_emails"]["options"]}
        self.assertEqual(options["business_id"]["type"], "list")
        self.assertEqual(options["dry_run"]["type"], "flag")
        self.assertNotIn("verbosity", options)
        self.assertNotIn("reacher_container", options)

    def test_start_with_options(self):
        response, launch = self.start({"command": "verify_emails",
                                       "options": {"limit": 5, "dry_run": True, "business_id": [3, 4], "recheck": False}})
        self.assertEqual(response.status_code, 202, response.content)
        run = PipelineRun.objects.get()
        self.assertEqual(run.arguments, ["--limit", "5", "--dry-run", "--business-id", "3", "--business-id", "4"])
        self.assertEqual((run.status, run.created_by), ("queued", self.user))
        launch.assert_called_once_with(run.pk)
        self.assertEqual(response.json()["command_line"], "python manage.py verify_emails --limit 5 --dry-run --business-id 3 --business-id 4")

    def test_start_with_raw_arguments(self):
        response, _ = self.start({"command": "send_emails", "arguments": ["--limit", "2"]})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(PipelineRun.objects.get().arguments, ["--limit", "2"])

    def test_invalid_runs_are_refused(self):
        for payload in ({"command": "migrate"}, {"command": "verify_emails", "options": {"nope": 1}},
                        {"command": "verify_emails", "options": {"limit": "x"}},
                        {"command": "verify_emails", "options": {"dry_run": "yes"}},
                        {"command": "send_emails", "arguments": ["--send", "--unknown"]}):
            response, launch = self.start(payload)
            self.assertEqual(response.status_code, 400, payload)
            launch.assert_not_called()
        self.assertFalse(PipelineRun.objects.exists())

    def test_campaign_fetch_shortcut(self):
        campaign = make_campaign()
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(f"/api/campaigns/{campaign.pk}/fetch-businesses/", {"limit": 7}, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(PipelineRun.objects.get().arguments, ["--campaign-id", str(campaign.pk), "--limit", "7"])

    def test_cancel_and_delete(self):
        run = PipelineRun.objects.create(command="send_emails")
        self.assertEqual(self.api.delete(f"/api/runs/{run.pk}/").status_code, 409)  # not finished
        response = self.api.post(f"/api/runs/{run.pk}/cancel/")
        self.assertEqual(response.json()["run"]["status"], "cancelled")
        self.assertEqual(self.api.delete(f"/api/runs/{run.pk}/").status_code, 204)

    def test_list_hides_output_detail_shows_it(self):
        run = PipelineRun.objects.create(command="send_emails", status="succeeded", output="hello")
        self.assertNotIn("output", self.api.get("/api/runs/").json()["results"][0])
        self.assertEqual(self.api.get(f"/api/runs/{run.pk}/").json()["output"], "hello")
