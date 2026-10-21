"""Finding and verifying contacts with Hunter (Settings: "Find and verify contacts with" = Hunter), against a fake
Hunter API: the real one is never called from a test."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from pipeline.models import BusinessContact, Prospect, SendingSettings
from pipeline.services import hunter
from tests.support import make_business, make_campaign, run_command

KEY = "hunter-test-key"


def person(value, first, last, position, confidence=90, seniority=None, kind="personal"):
    return {"value": value, "type": kind, "confidence": confidence, "first_name": first, "last_name": last,
            "position": position, "seniority": seniority, "department": None, "linkedin": None,
            "sources": [{"uri": f"https://example.org/{first or 'x'}"}]}


class FakeHunter:
    """Hunter's v2 API: domain-search answers `domains[domain or company]`; email-verifier answers by local part."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.domains: dict[str, dict] = {}
        self.statuses = {"owner": {"status": "valid", "result": "deliverable", "score": 97, "smtp_check": True},
                         "manager": {"status": "accept_all", "result": "risky", "score": 70, "accept_all": True},
                         "gone": {"status": "invalid", "result": "undeliverable", "score": 0, "mx_records": True},
                         "free": {"status": "webmail", "result": "risky", "score": 60, "webmail": True}}
        self.pending: dict[str, int] = {}  # local part -> times to answer 202 (still verifying) first
        self.fail_with: int | None = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                url = urlsplit(self.path)
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                fake.calls.append((url.path, params))
                if self.headers.get("X-API-KEY") != KEY:
                    return self.answer(401, {"errors": [{"id": "authentication_failed", "details": "No user found"}]})
                if fake.fail_with:
                    return self.answer(fake.fail_with, {"errors": [{"details": "Usage limit reached"}]})
                if url.path.endswith("/domain-search"):
                    data = fake.domains.get(params.get("domain") or params.get("company"), {"emails": []})
                    return self.answer(200, {"data": data})
                local = params["email"].partition("@")[0]
                if fake.pending.get(local):
                    fake.pending[local] -= 1
                    return self.answer(202, {"data": {"status": None}})
                return self.answer(200, {"data": {"email": params["email"], "regexp": True,
                                                   **fake.statuses.get(local, {"status": "unknown"})}})

            def answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v2"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HunterCase(TestCase):
    require_score = False  # the AI score gate is tested in test_email_scoring

    def setUp(self):
        self.hunter = FakeHunter()
        self.addCleanup(self.hunter.close)
        env = mock.patch.dict(os.environ, {"HUNTER_API_KEY": KEY, "HUNTER_BASE_URL": self.hunter.url})
        env.start()
        self.addCleanup(env.stop)
        SendingSettings.objects.update_or_create(pk=1, defaults={"contact_provider": "hunter",
                                                                 "email_scoring_enabled": self.require_score})
        no_reacher = mock.patch("pipeline.services.verification.prepare_reacher",
                                side_effect=AssertionError("Reacher must not be used in Hunter mode"))
        no_reacher.start()
        self.addCleanup(no_reacher.stop)
        no_search = mock.patch("pipeline.services.searxng.search",
                               side_effect=AssertionError("SearXNG must not be used in Hunter mode"))
        no_search.start()
        self.addCleanup(no_search.stop)
        self.campaign = make_campaign()
        self.clinic = make_business(self.campaign, "Smile Studio | DHA", score=80, website_url="https://smile.pk")
        self.hunter.domains["smile.pk"] = {"domain": "smile.pk", "pattern": "{first}", "emails": [
            person("info@smile.pk", None, None, None, confidence=99, kind="generic"),
            person("sara@smile.pk", "Sara", "Ali", "Receptionist"),
            person("manager@smile.pk", "Omar", "Khan", "Practice Manager", confidence=80),
            person("owner@smile.pk", "Ayesha", "Raza", "Owner & Principal Dentist", confidence=95, seniority="executive"),
            person("bilal@smile.pk", "Bilal", "Shah", "Marketing", confidence=70, seniority="senior"),
        ]}


class FindWithHunterTests(HunterCase):
    def test_people_at_the_domain_become_contacts_best_role_first(self):
        result = run_command("find_stakeholders", "--business-id", self.clinic.pk)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Hunter (domain smile.pk): 5 email(s), pattern {first}", result.stdout)
        self.assertEqual(self.hunter.calls[0], ("/v2/domain-search", {"domain": "smile.pk", "limit": "10"}))
        contacts = list(BusinessContact.objects.filter(business=self.clinic).order_by("-is_primary", "id"))
        self.assertEqual([(c.email, c.is_primary) for c in contacts][:1], [("owner@smile.pk", True)])
        emails = {c.email for c in contacts}
        self.assertEqual(emails, {"owner@smile.pk", "manager@smile.pk", "bilal@smile.pk"})  # no receptionist, no info@
        owner = contacts[0]
        self.assertEqual((owner.name, owner.first_name, owner.job_title, owner.role_type, owner.email_source,
                          owner.email_status, float(owner.confidence)),
                         ("Ayesha Raza", "Ayesha", "Owner & Principal Dentist", "owner", "hunter", "unverified", 0.95))
        self.assertIn("found by Hunter's domain search (95% confidence", owner.discovery_reasoning)
        self.assertEqual(owner.source_urls, ["https://example.org/Ayesha"])

    def test_without_a_website_the_company_name_is_searched_and_a_rerun_replaces_the_contacts(self):
        no_site = make_business(self.campaign, "City Dental (Gulberg)", score=80)
        self.hunter.domains["City Dental"] = {"emails": [person("hello@citydental.pk", None, None, None,
                                                                kind="generic")]}
        run_command("find_stakeholders", "--business-id", no_site.pk)
        self.assertEqual(self.hunter.calls[0][1], {"company": "City Dental", "limit": "10"})
        self.assertEqual(list(BusinessContact.objects.filter(business=no_site).values_list("email", "job_title")),
                         [("hello@citydental.pk", "General address")])  # no person: the general address
        manual = BusinessContact.objects.create(business=no_site, name="Dr Asad", email="dr@citydental.pk",
                                                email_source="manual")
        self.hunter.domains["City Dental"] = {"emails": []}
        run_command("find_stakeholders", "--business-id", no_site.pk, "--redo")
        self.assertEqual(list(BusinessContact.objects.filter(business=no_site)), [manual])  # hand-added stays

    def test_a_refused_key_or_used_up_credits_stop_the_run(self):
        self.hunter.fail_with = 429
        result = run_command("find_stakeholders", "--business-id", self.clinic.pk)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Hunter's limit is reached", result.stdout)
        self.assertFalse(BusinessContact.objects.exists())
        self.hunter.fail_with = None
        with mock.patch.dict(os.environ, {"HUNTER_API_KEY": "wrong"}):
            self.assertIn("Hunter refused the key", run_command("find_stakeholders", "--business-id",
                                                                self.clinic.pk).stdout)
        with mock.patch.dict(os.environ, {"HUNTER_API_KEY": ""}):
            self.assertIn("HUNTER_API_KEY is not set", run_command("find_stakeholders").stderr)


class VerifyWithHunterTests(HunterCase):
    def setUp(self):
        super().setUp()
        run_command("find_stakeholders", "--business-id", self.clinic.pk)
        self.hunter.calls.clear()

    def test_each_contacts_address_is_verified_and_stored_like_reachers_results(self):
        BusinessContact.objects.filter(email="bilal@smile.pk").update(email="free@gmail.com")
        with mock.patch.object(hunter, "VERIFY_RETRY_WAIT", 0):
            self.hunter.pending["owner"] = 1  # Hunter is still verifying: asked again
            result = run_command("verify_emails", "--business-id", self.clinic.pk)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Verifying with Hunter", result.stdout)
        rows = {p.email: (p.email_status, p.verdict, p.outreach_status, p.email_verification_provider)
                for p in Prospect.objects.all()}
        self.assertEqual(rows, {
            "owner@smile.pk": ("deliverable", "deliverable", "ready", "hunter"),
            "manager@smile.pk": ("risky", "catch_all", "needs_review", "hunter"),
            "free@gmail.com": ("unknown", "webmail", "needs_review", "hunter"),
        })
        owner = Prospect.objects.get(email="owner@smile.pk")
        self.assertEqual((owner.verification_details["score"], owner.verification_details["status"]), (97, "valid"))
        verifier_calls = [params["email"] for path, params in self.hunter.calls if path.endswith("email-verifier")]
        self.assertEqual(verifier_calls.count("owner@smile.pk"), 2)  # the 202, then the answer
        self.assertEqual(BusinessContact.objects.get(email="owner@smile.pk").email_status, "deliverable")

    def test_only_the_contacts_own_address_is_checked_and_decided_ones_are_not_paid_for_again(self):
        BusinessContact.objects.filter(email="owner@smile.pk").update(
            candidate_emails=[{"email": "gone@smile.pk", "pattern": "x", "confidence": 0.1}])
        run_command("verify_emails", "--business-id", self.clinic.pk)
        checked = [params["email"] for _, params in self.hunter.calls]
        self.assertNotIn("gone@smile.pk", checked)  # guesses cost credits: own address only by default
        self.hunter.calls.clear()
        run_command("verify_emails", "--business-id", self.clinic.pk)
        self.assertEqual(self.hunter.calls, [])  # every address already has a conclusive answer
        run_command("verify_emails", "--business-id", self.clinic.pk, "--max-candidates", "2", "--contact-id",
                    BusinessContact.objects.get(email="owner@smile.pk").pk)
        self.assertEqual(Prospect.objects.get(email="gone@smile.pk").email_status, "undeliverable")

    def test_a_stop_from_hunter_keeps_what_was_done(self):
        self.hunter.fail_with = 403
        result = run_command("verify_emails", "--business-id", self.clinic.pk)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Hunter's limit is reached", result.stdout)
        self.assertFalse(Prospect.objects.exists())


class HunterSettingTests(HunterCase):
    def test_settings_page_api_and_dashboard(self):
        user = get_user_model().objects.create_superuser("h", "h@example.org", "pw")
        self.client.force_login(user)
        page = self.client.get(reverse("admin:pipeline_sendingsettings_change", args=[1]))
        self.assertContains(page, "Find and verify contacts with")
        self.assertContains(page, "Hunter (domain search and email verifier)")
        with mock.patch.dict(os.environ, {"HUNTER_API_KEY": "", "VERIFY_HELO": ""}):
            dashboard = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("HUNTER_API_KEY is not set", dashboard)
        self.assertNotIn("VERIFY_HELO is not set", dashboard)  # Reacher is not used
        SendingSettings.objects.filter(pk=1).update(contact_provider="builtin")
        with mock.patch.dict(os.environ, {"HUNTER_API_KEY": "", "VERIFY_HELO": ""}):
            dashboard = self.client.get(reverse("admin:index")).content.decode()
        self.assertNotIn("HUNTER_API_KEY", dashboard)
        self.assertIn("VERIFY_HELO is not set", dashboard)
