import json
import os
import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from pipeline.models import BusinessWebsiteProfile, Email, EmailOpenEvent, Prospect
from pipeline.services import generation as g
from tests.support import ROOT, make_business, make_campaign, make_contact, run_command

BASE = "https://ref.supabase.co/functions/v1/email/footer"
EDGE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")  # same as supabase/functions/email/handler.ts


def fake_completion(content):
    client = mock.Mock()
    client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=c))]) for c in content
    ]
    return client


class TrackingTests(unittest.TestCase):
    def test_tokens_are_unique_random_and_accepted_by_the_edge_function(self):
        tokens = {g.new_tracking_token() for _ in range(2000)}
        self.assertEqual(len(tokens), 2000)
        for token in list(tokens)[:200]:
            self.assertRegex(token, g.TOKEN_PATTERN)
            self.assertRegex(token, EDGE_TOKEN_PATTERN)
        self.assertEqual(g.TOKEN_PATTERN.pattern, EDGE_TOKEN_PATTERN.pattern)

    def test_edge_function_uses_the_same_token_pattern(self):
        handler = (ROOT / "supabase" / "functions" / "email" / "handler.ts").read_text()
        self.assertIn(f"/{g.TOKEN_PATTERN.pattern}/", handler)

    def test_base_url_from_env_or_supabase_host(self):
        with mock.patch.dict(os.environ, {"EMAIL_TRACKING_BASE_URL": "https://track.example.dev/email/footer/"}):
            self.assertEqual(g.tracking_base_url(None), "https://track.example.dev/email/footer")
        with mock.patch.dict(os.environ, {"EMAIL_TRACKING_BASE_URL": ""}):
            self.assertEqual(g.tracking_base_url("postgresql://postgres:pw@db.abc123.supabase.co:5432/postgres"),
                             "https://abc123.supabase.co/functions/v1/email/footer")
            self.assertIsNone(g.tracking_base_url("postgresql://u:p@localhost:5432/db"))
            self.assertIsNone(g.tracking_base_url(None))

    def test_tracking_url_contains_only_the_token(self):
        token = g.new_tracking_token()
        url = g.tracking_url(BASE, token)
        self.assertEqual(url, f"{BASE}/{token}")
        self.assertNotIn("@", url)


class RenderTests(unittest.TestCase):
    def test_paragraphs_line_breaks_and_footer(self):
        out = g.render_html("Hi Ali,\n\nLine one\nline two\n\n\nThanks", f"{BASE}/tok")
        self.assertIn("<p>Hi Ali,</p>", out)
        self.assertIn("<p>Line one<br>line two</p>", out)
        self.assertIn("<p>Thanks</p>", out)
        self.assertIn(f'<img src="{BASE}/tok" alt="Axioware" width="600"', out)
        self.assertEqual(out.count("<img"), 1)

    def test_model_output_is_escaped(self):
        out = g.render_html('<script>alert(1)</script> & "x"', f'{BASE}/a"b')
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&amp;", out)
        self.assertIn('src="' + BASE + '/a&quot;b"', out)


class PromptAndContextTests(unittest.TestCase):
    def test_prompt_is_the_real_axioware_prompt(self):
        self.assertFalse(g.PROMPT_IS_PLACEHOLDER)
        self.assertNotIn("PLACEHOLDER", g.SYSTEM_PROMPT)
        for must in ("Axioware", "Ava", "axioware.tech/dental-agent", "Never invent", "first_name", "website_findings"):
            self.assertIn(must, g.SYSTEM_PROMPT)

    def test_prompt_mentions_every_context_field(self):
        row = {"business_name": "B", "category": None, "city": None, "country": None, "website_url": None,
               "google_rating": None, "google_review_count": None, "contact_name": "A B", "first_name": "A",
               "job_title": None, "role_type": None, "qualification_score": 1, "qualification_reasons": [],
               "outreach_facts": [], "research_summary": None, "scraped_pages": []}
        for key in g.prospect_context(row):
            self.assertIn(key, g.SYSTEM_PROMPT, key)

    def test_website_findings_keep_business_facts_only(self):
        pages = [
            {"relevant_findings": ["Dental clinic in Lahore, Pakistan.", "Shows 155 Google reviews and a 5-star rating widget on-page.",
                                   "About Us page likely contains clinic background, credentials, and team details.",
                                   "Current page content is empty/blank after cleaning", "Five-page limit has not been reached yet"]},
            {"relevant_findings": ["Phone: +92 300 0933343; emails include admin@b.pk", "Hours: Monday-Saturday 11:00 AM to 09:00 PM.",
                                   "Clinic address: Shop #12, Model Town, Lahore, Punjab.",
                                   "Address: Shop #12, Model Town, Lahore, Punjab, Pakistan."]},
            None, {"relevant_findings": None}, {},
        ]
        self.assertEqual(g.website_findings(pages), [
            "Dental clinic in Lahore, Pakistan.", "Shows 155 Google reviews and a 5-star rating widget on-page.",
            "Hours: Monday-Saturday 11:00 AM to 09:00 PM.", "Clinic address: Shop #12, Model Town, Lahore, Punjab."])
        self.assertEqual(g.website_findings(None), [])
        many = [{"relevant_findings": [f"Offers treatment number {i} for patients" for i in range(40)]}]
        self.assertLessEqual(len(g.website_findings(many)), g.MAX_WEBSITE_FINDINGS)

    def test_job_title_annotations_are_removed(self):
        self.assertEqual(g.clean_job_title("Principal (practice named after them)"), "Principal")
        self.assertEqual(g.clean_job_title("Owner"), "Owner")
        self.assertIsNone(g.clean_job_title("(guess)"))
        self.assertIsNone(g.clean_job_title(None))

    def test_sender_name_comes_from_smtp_from_name(self):
        row = {"business_name": "B", "category": None, "city": None, "country": None, "website_url": None,
               "google_rating": None, "google_review_count": None, "contact_name": "A B", "first_name": "A",
               "job_title": None, "role_type": None, "qualification_score": 1, "qualification_reasons": [],
               "outreach_facts": [], "research_summary": None}
        with mock.patch.dict(os.environ, {"SMTP_FROM_NAME": "Abdul Rauf"}):
            self.assertEqual(g.prospect_context(row)["sender"], {"name": "Abdul Rauf", "company": "Axioware"})
        with mock.patch.dict(os.environ, {"SMTP_FROM_NAME": ""}):
            self.assertEqual(g.prospect_context(row)["sender"]["name"], "")


class FooterImageTests(unittest.TestCase):
    def test_built_footer_matches_the_declared_width_at_2x(self):
        import struct
        png = (ROOT / "assets" / "email-footer.png").read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual(width, g.FOOTER_WIDTH * 2)
        self.assertLess(height, width)


class GenerationTests(unittest.TestCase):
    CONTEXT = {"business": {"name": "Smile Solutions"}}

    def test_valid_output(self):
        client = fake_completion(['{"subject": "  Hello   there ", "body": "Body text\\n"}'])
        draft = g.generate_draft(client, "m", self.CONTEXT)
        self.assertEqual((draft.subject, draft.body), ("Hello there", "Body text"))
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "m")
        self.assertTrue(kwargs["response_format"]["json_schema"]["strict"])
        self.assertEqual(kwargs["messages"][0]["content"], g.SYSTEM_PROMPT)
        self.assertEqual(json.loads(kwargs["messages"][1]["content"])["business"]["name"], "Smile Solutions")

    def test_retries_with_feedback_then_succeeds(self):
        client = fake_completion(["not json", '{"subject": "S", "body": "B"}'])
        self.assertEqual(g.generate_draft(client, "m", self.CONTEXT).subject, "S")
        second = json.loads(client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"])
        self.assertIn("subject", second["validation_feedback"])

    def test_blank_or_oversized_output_is_rejected(self):
        for bad in ('{"subject": "   ", "body": "B"}', '{"subject": "S", "body": "  "}',
                    json.dumps({"subject": "x" * 300, "body": "B"}), json.dumps({"subject": "S", "body": "b" * 6000})):
            with self.assertRaises(g.GenerationError, msg=bad):
                g.generate_draft(fake_completion([bad] * 3), "m", self.CONTEXT)

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(g.GenerationError):
            g.generate_draft(fake_completion(['{"subject": "S", "body": "B", "to": "x@y.z"}'] * 3), "m", self.CONTEXT)

    def test_context_has_no_email_addresses(self):
        row = {"business_name": "B", "category": "Dentist", "city": "Lahore", "country": "PK", "website_url": "http://b.pk",
               "google_rating": None, "google_review_count": 5, "contact_name": "Shoaib Ahmed", "first_name": "Shoaib",
               "job_title": "Owner", "role_type": "owner", "qualification_score": 85, "qualification_reasons": ["r"],
               "outreach_facts": ["f"], "research_summary": "s", "recipient": "shoaib@b.pk"}
        row["scraped_pages"] = [{"relevant_findings": ["Phone: +92 300 0933343; email info@b.pk", "Hours: 9-5"]}]
        context = g.prospect_context(row)
        self.assertNotIn("@", json.dumps(context))
        self.assertNotIn("0933343", json.dumps(context))
        self.assertEqual(context["website_findings"], ["Hours: 9-5"])
        self.assertEqual(context["contact"]["first_name"], "Shoaib")
        self.assertEqual(context["outreach_facts"], ["f"])


# ---------------------------------------------------------------- end to end


class FakeLLM:
    """OpenAI-compatible /chat/completions; the reply mentions the business so tests can tell drafts apart."""

    def __init__(self):
        self.mode = "ok"
        self.calls = 0
        self.system_prompts = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls += 1
                fake.system_prompts.append(request["messages"][0]["content"])
                user = json.loads(request["messages"][1]["content"])
                name = user["business"]["name"]
                if fake.mode == "bad" or (fake.mode == "bad-for-b2" and name == "Business 2"):
                    content = "not json"
                else:
                    content = json.dumps({"subject": f"Idea for {name} #{fake.calls}",
                                          "body": f"Hi {user['contact']['first_name']},\n\nNote for {name}."})
                payload = {"id": "x", "object": "chat.completion", "created": 0, "model": request["model"],
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": content}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GenerateCase:
    def setUp(self):
        self.llm = FakeLLM()
        self.addCleanup(self.llm.close)
        campaign = make_campaign()
        self.b, self.c = [None], [None]
        for i in range(1, 5):
            business = make_business(campaign, f"Business {i}", city="Lahore")
            self.b.append(business)
            self.c.append(make_contact(business, name=f"Person {i}", first_name=f"P{i}", job_title="Owner"))
        BusinessWebsiteProfile.objects.create(business=self.b[1], status="completed", qualification_score=90,
                                              qualification_reasons=["strong"])

    def prospect(self, index, email, **cols):
        values = {"email_status": "deliverable", "outreach_status": "ready", "do_not_contact": False,
                  "qualification_score": 50, **cols}
        return Prospect.objects.create(business=self.b[index], contact=self.c[index], email=email, **values).pk

    def emails(self):
        return list(Email.objects.order_by("id").values())

    def run_cli(self, *args, expect=None, **env_overrides):
        env = {"OPENAI_API_KEY": "test", "OPENAI_BASE_URL": self.llm.url, "GROQ_API_KEY": "", "GROK_API_KEY": "",
               "OPENAI_MODEL": "fake-model", "EMAIL_TRACKING_BASE_URL": BASE, **env_overrides}
        result = run_command("generate_emails", *args, env=env)
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def make_email(self, sent=True):
        self.prospect(1, "a@b1.org")
        self.run_cli(expect=0)
        if sent:
            Email.objects.update(status="sent", sent_at=time_now())
        return self.emails()[0]

    def record(self, token, ua="Mail/1"):
        with connection.cursor() as cursor:
            cursor.execute("select record_email_open(%s, %s)", [token, ua])
            return cursor.fetchone()[0]


def time_now():
    from django.utils import timezone

    return timezone.now()


class GenerateEmailsEndToEndTests(GenerateCase, TestCase):
    def test_one_draft_per_ready_prospect(self):
        p1 = self.prospect(1, "a@b1.org", qualification_score=90)
        p2 = self.prospect(2, "a@b2.org")
        out = self.run_cli(expect=0).stdout
        rows = {r["prospect_id"]: r for r in self.emails()}
        self.assertEqual(set(rows), {p1, p2})
        first = rows[p1]
        self.assertEqual((first["recipient"], first["business_id"], first["contact_id"]), ("a@b1.org", self.b[1].pk, self.c[1].pk))
        self.assertEqual((first["status"], first["sequence_step"], first["open_count"]), ("in_review", 1, 0))
        self.assertEqual((first["generation_provider"], first["generation_model"]), ("openai", "fake-model"))
        self.assertIsNotNone(first["generated_at"])
        self.assertIsNone(first["sent_at"])
        self.assertIn("Business 1", first["subject"])
        self.assertIn("Hi P1", first["body_text"])
        self.assertRegex(first["tracking_token"], EDGE_TOKEN_PATTERN)
        self.assertIn(f'src="{BASE}/{first["tracking_token"]}"', first["body_html"])
        self.assertNotEqual(rows[p1]["tracking_token"], rows[p2]["tracking_token"])
        self.assertNotIn("prompt is still a placeholder", out)
        self.assertIn("2 email(s) saved for review", out)

    def test_ineligible_prospects_are_skipped(self):
        self.prospect(1, "a@b1.org", email_status="undeliverable")
        self.prospect(2, "a@b2.org", outreach_status="needs_review")
        self.prospect(3, "a@b3.org", do_not_contact=True)
        self.assertIn("No prospects need an email", self.run_cli(expect=0).stdout)
        self.assertEqual(self.emails(), [])

    def test_rerun_skips_and_regenerate_rewrites_drafts_keeping_the_token(self):
        self.prospect(1, "a@b1.org")
        self.run_cli(expect=0)
        before = self.emails()[0]
        self.assertIn("No prospects need an email", self.run_cli(expect=0).stdout)
        self.run_cli("--regenerate", expect=0)
        after = self.emails()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["tracking_token"], before["tracking_token"])
        self.assertNotEqual(after[0]["subject"], before["subject"])

    def test_regenerate_never_touches_a_sent_email(self):
        self.prospect(1, "a@b1.org")
        self.run_cli(expect=0)
        Email.objects.update(status="sent", sent_at=time_now())
        before = self.emails()[0]
        self.assertIn("No prospects need an email", self.run_cli("--regenerate", expect=0).stdout)
        self.assertEqual(self.emails()[0]["subject"], before["subject"])

    def test_regenerate_rewrites_rejected_back_to_review_but_never_approved(self):
        self.prospect(1, "a@b1.org")
        self.prospect(2, "a@b2.org")
        self.run_cli(expect=0)
        Email.objects.filter(business=self.b[1]).update(status="rejected", review_note="too long")
        Email.objects.filter(business=self.b[2]).update(status="approved")
        before = {r["business_id"]: r for r in self.emails()}
        self.run_cli("--regenerate", expect=0)
        after = {r["business_id"]: r for r in self.emails()}
        first, second = self.b[1].pk, self.b[2].pk
        self.assertEqual((after[first]["status"], after[first]["review_note"]), ("in_review", None))
        self.assertNotEqual(after[first]["subject"], before[first]["subject"])
        self.assertEqual(after[second]["subject"], before[second]["subject"])  # approved: untouched
        self.assertEqual(after[second]["status"], "approved")

    def test_dry_run_writes_nothing(self):
        self.prospect(1, "a@b1.org")
        out = self.run_cli("--dry-run", expect=0).stdout
        self.assertIn("Note for Business 1", out)
        self.assertEqual(self.emails(), [])

    def test_bad_model_output_fails_one_prospect_and_continues(self):
        self.prospect(1, "a@b1.org")
        self.prospect(2, "a@b2.org")
        self.llm.mode = "bad-for-b2"
        result = self.run_cli(expect=1)
        self.assertIn("1 email(s) saved for review, 1 failed", result.stdout)
        self.assertEqual([r["business_id"] for r in self.emails()], [self.b[1].pk])
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_unreachable_llm_fails_cleanly(self):
        self.prospect(1, "a@b1.org")
        result = self.run_cli(expect=1, OPENAI_BASE_URL="http://127.0.0.1:9/v1")
        self.assertIn("failed", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(self.emails(), [])

    def test_filters(self):
        self.prospect(1, "a@b1.org")
        p2 = self.prospect(2, "a@b2.org")
        self.prospect(3, "a@b3.org")
        self.run_cli("--prospect-id", p2, expect=0)
        self.assertEqual([r["prospect_id"] for r in self.emails()], [p2])
        self.run_cli("--business-id", self.b[3].pk, expect=0)
        self.assertEqual(sorted(r["business_id"] for r in self.emails()), [self.b[2].pk, self.b[3].pk])
        self.run_cli("--limit", "1", expect=0)
        self.assertEqual(len(self.emails()), 3)

    def test_missing_configuration(self):
        no_llm = self.run_cli(OPENAI_API_KEY="", GROQ_API_KEY="", GROK_API_KEY="")
        self.assertEqual(no_llm.returncode, 1)
        self.assertIn("GROQ_API_KEY", no_llm.stderr)
        no_base = self.run_cli(EMAIL_TRACKING_BASE_URL="", DATABASE_URL="postgresql://u:p@localhost/db")
        self.assertEqual(no_base.returncode, 1)
        self.assertIn("EMAIL_TRACKING_BASE_URL", no_base.stderr)

    # --------------------------------------------------------- open tracking (database side)

    def test_first_open_marks_opened_and_later_opens_count(self):
        email = self.make_email()
        self.assertTrue(self.record(email["tracking_token"]))
        first = self.emails()[0]
        self.assertEqual((first["status"], first["open_count"]), ("opened", 1))
        self.assertIsNotNone(first["first_opened_at"])
        self.assertEqual(first["first_opened_at"], first["last_opened_at"])
        self.assertTrue(self.record(email["tracking_token"], "Other"))
        second = self.emails()[0]
        self.assertEqual(second["open_count"], 2)
        self.assertEqual(second["first_opened_at"], first["first_opened_at"])
        self.assertGreaterEqual(second["last_opened_at"], first["last_opened_at"])
        self.assertEqual(list(EmailOpenEvent.objects.order_by("id").values_list("user_agent", flat=True)), ["Mail/1", "Other"])

    def test_draft_and_unknown_tokens_record_nothing(self):
        email = self.make_email(sent=False)
        self.assertFalse(self.record(email["tracking_token"]))
        self.assertFalse(self.record("x" * 43))
        row = self.emails()[0]
        self.assertEqual((row["status"], row["open_count"], row["first_opened_at"]), ("in_review", 0, None))
        self.assertEqual(EmailOpenEvent.objects.count(), 0)

    def test_failed_status_is_not_overwritten_by_an_open(self):
        email = self.make_email()
        Email.objects.update(status="failed")
        self.assertTrue(self.record(email["tracking_token"]))
        self.assertEqual(self.emails()[0]["status"], "failed")

    def test_rate_limit_window_moves_on(self):
        email = self.make_email()
        for _ in range(10):
            self.assertTrue(self.record(email["tracking_token"]))
        self.assertFalse(self.record(email["tracking_token"]))
        with connection.cursor() as cursor:  # a minute passes
            cursor.execute("update email_open_events set opened_at = opened_at - interval '2 minutes'")
        self.assertTrue(self.record(email["tracking_token"]))
        self.assertEqual(self.emails()[0]["open_count"], 11)

    def test_rate_limit_is_per_email(self):
        email = self.make_email()
        for _ in range(10):
            self.record(email["tracking_token"])
        self.prospect(2, "a@b2.org")
        self.run_cli("--business-id", self.b[2].pk, expect=0)
        Email.objects.filter(business=self.b[2]).update(status="sent", sent_at=time_now())
        other = Email.objects.get(business=self.b[2])
        self.assertTrue(self.record(other.tracking_token))

    def test_user_agent_is_truncated_in_the_database(self):
        email = self.make_email()
        self.record(email["tracking_token"], "u" * 2000)
        self.assertEqual(len(EmailOpenEvent.objects.get().user_agent), 500)

    def test_schema_constraints(self):
        self.make_email(sent=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Email.objects.update(status="bogus")
        original = Email.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():  # tracking tokens are unique
            Email.objects.create(prospect=original.prospect, business=original.business, contact=original.contact,
                                 sequence_step=2, recipient=original.recipient, subject="s", body_text="b",
                                 body_html="h", tracking_token=original.tracking_token)
        with connection.cursor() as cursor:
            cursor.execute("select relname, relrowsecurity from pg_class where relname in ('emails', 'email_open_events')")
            self.assertEqual(dict(cursor.fetchall()), {"emails": True, "email_open_events": True})
        Prospect.objects.all().delete()  # cascades
        self.assertEqual(self.emails(), [])


class ConcurrentOpenTests(GenerateCase, TransactionTestCase):
    def test_concurrent_opens_are_counted_exactly_up_to_the_limit(self):
        email = self.make_email()
        results = []

        def open_once():
            try:
                results.append(self.record(email["tracking_token"]))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=open_once) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.emails()[0]["open_count"], 10)  # 10 per email per minute, enforced in the database
        self.assertEqual(results.count(True), 10)
        self.assertEqual(EmailOpenEvent.objects.count(), 10)


class MigrationTests(TransactionTestCase):
    def test_migrations_reverse_and_apply_again(self):
        from django.core.management import call_command

        call_command("migrate", "pipeline", "0001", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from pg_proc where proname = 'record_email_open'")
            self.assertEqual(cursor.fetchone()[0], 0)
        call_command("migrate", "pipeline", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from pg_proc where proname = 'record_email_open'")
            self.assertEqual(cursor.fetchone()[0], 1)
