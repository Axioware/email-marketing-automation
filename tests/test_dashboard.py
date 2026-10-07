import os
import re
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.fake_smtp import FakeSmtpServer
from tests.test_generate_emails import FakeLLM
from tests.test_verify_e2e import ROOT, docker_ok, free_port

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from dashboard.app import create_app  # noqa: E402
import generate_emails as g  # noqa: E402

BASE = "https://ref.supabase.co/functions/v1/email/footer"


@unittest.skipUnless(docker_ok(), "Docker is not available")
class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"emailtest-pg-{uuid.uuid4().hex[:8]}"
        cls.port = free_port()
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.name, "-e", "POSTGRES_PASSWORD=test",
                        "-p", f"127.0.0.1:{cls.port}:5432", "postgres:16-alpine"], check=True, capture_output=True)
        cls.url = f"postgresql+psycopg2://postgres:test@127.0.0.1:{cls.port}/postgres"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if subprocess.run(["docker", "exec", cls.name, "pg_isready", "-U", "postgres"], capture_output=True).returncode == 0:
                try:
                    create_engine(cls.url).connect().close()
                    break
                except Exception:
                    pass
            time.sleep(1)
        migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                                  env={**os.environ, "DATABASE_URL": cls.url}, capture_output=True, text=True)
        if migrated.returncode != 0:
            raise RuntimeError(migrated.stderr)
        cls.engine = create_engine(cls.url)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)

    def setUp(self):
        self.smtp = FakeSmtpServer()
        self.llm = FakeLLM()
        self.addCleanup(self.smtp.close)
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(os.environ, {
            "EMAIL_TRACKING_BASE_URL": BASE, "OPENAI_API_KEY": "test", "OPENAI_BASE_URL": self.llm.url,
            "OPENAI_MODEL": "fake-model", "GROQ_API_KEY": "", "GROK_API_KEY": "",
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.port), "SMTP_SECURITY": "none",
            "SMTP_USERNAME": "user@x.org", "SMTP_PASSWORD": "secret", "SMTP_FROM_EMAIL": "user@x.org",
            "SMTP_FROM_NAME": "Sender", "SMTP_REPLY_TO": "",
        })
        env.start()
        self.addCleanup(env.stop)
        with self.engine.begin() as c:
            c.execute(text("truncate email_open_events, emails, prospects, business_contacts, business_website_profiles, "
                           "businesses, discovery_campaigns restart identity cascade"))
            c.execute(text("insert into discovery_campaigns (id, name) values (1, 't')"))
        self.client = TestClient(create_app(database_url=self.url))

    # ------------------------------------------------------------- helpers

    def email(self, status="in_review", body="Hi Ali,\n\nA short note.", subject="Hello there", recipient=None, **prospect):
        with self.engine.begin() as c:
            n = c.execute(text("select count(*) from businesses")).scalar() + 1
            c.execute(text("insert into businesses (id, discovery_campaign_id, name, city) values (:i, 1, :n, 'Lahore')"),
                      {"i": n, "n": f"Clinic {n}"})
            c.execute(text("insert into business_contacts (id, business_id, name, first_name) values (:i, :i, 'Ali Khan', 'Ali')"), {"i": n})
            c.execute(text("insert into business_website_profiles (business_id, status, qualification_score, qualification_reasons, scraped_pages) "
                           "values (:i, 'completed', 80, '[\"Strong reviews\"]'::jsonb, '[{\"relevant_findings\": [\"Hours: 9-5\"]}]'::jsonb)"), {"i": n})
            cols = {"email_status": "deliverable", "outreach_status": "ready", "do_not_contact": False, **prospect}
            recipient = recipient or f"ali{n}@clinic{n}.org"
            pid = c.execute(text("insert into prospects (business_id, contact_id, email, email_status, outreach_status, do_not_contact) "
                                 "values (:i, :i, :e, :email_status, :outreach_status, :do_not_contact) returning id"),
                            {"i": n, "e": recipient, **cols}).scalar_one()
            token = g.new_tracking_token()
            return c.execute(text("insert into emails (prospect_id, business_id, contact_id, recipient, subject, body_text, body_html, "
                                  "tracking_token, status) values (:p, :i, :i, :r, :s, :bt, :bh, :t, :st) returning id"),
                             {"p": pid, "i": n, "r": recipient, "s": subject, "bt": g.render_text(body),
                              "bh": g.render_html(body, g.tracking_url(BASE, token)), "t": token, "st": status}).scalar_one()

    def row(self, email_id):
        with self.engine.connect() as c:
            return dict(c.execute(text("select * from emails where id=:i"), {"i": email_id}).one()._mapping)

    def csrf(self):
        return re.search(r'name="csrf" value="([^"]+)"', self.client.get("/emails?status=all").text).group(1)

    def post(self, path, **data):
        data.setdefault("csrf", self.csrf())
        return self.client.post(path, data=data, follow_redirects=False)

    def follow(self, response):
        self.assertEqual(response.status_code, 303, response.text)
        return self.client.get(response.headers["location"])

    # ------------------------------------------------------------- pages

    def test_list_shows_counts_and_filters_by_status(self):
        a = self.email("in_review", subject="Review me")
        self.email("approved", subject="Ready to go")
        page = self.client.get("/emails?status=in_review")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Review me", page.text)
        self.assertNotIn("Ready to go", page.text)
        self.assertIn(f'href="/emails/{a}"', page.text)
        self.assertIn("Ready to go", self.client.get("/emails?status=approved").text)
        both = self.client.get("/emails?status=all").text
        self.assertIn("Review me", both)
        self.assertIn("Ready to go", both)

    def test_search(self):
        self.email(subject="Alpha subject")
        self.email(subject="Beta subject")
        page = self.client.get("/emails?status=all&q=alpha").text
        self.assertIn("Alpha subject", page)
        self.assertNotIn("Beta subject", page)

    def test_home_redirects_to_review_queue(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/emails?status=in_review")

    def test_detail_preview_never_uses_the_tracking_url(self):
        eid = self.email()
        token = self.row(eid)["tracking_token"]
        page = self.client.get(f"/emails/{eid}")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(token, page.text)  # viewing in the dashboard must not count as an open
        self.assertIn("storage/v1/object/public/email-assets/footer.png", page.text)
        self.assertIn("Hours: 9-5", page.text)  # website findings shown to the reviewer
        self.assertIn("Strong reviews", page.text)

    def test_hostile_recipient_cannot_reach_inline_javascript(self):
        evil = "x');alert(document.cookie);//@evil.org"
        eid = self.email("approved", recipient=evil)
        page = self.client.get(f"/emails/{eid}").text
        # No database value may be interpolated into an inline event handler (browsers decode entities there).
        for handler in re.findall(r'\son\w+="([^"]*)"', page):
            self.assertNotIn("alert", handler)
            self.assertNotIn("evil.org", handler)
        self.assertRegex(page, r'data-recipient="x&#39;\);alert\(document\.cookie\);//@evil\.org"')

    def test_unknown_email_is_404(self):
        self.assertEqual(self.client.get("/emails/999999").status_code, 404)

    def test_model_or_db_text_is_escaped(self):
        eid = self.email(subject="<script>alert(1)</script>", body="Hi <b>there</b>")
        for page in (self.client.get("/emails?status=all").text, self.client.get(f"/emails/{eid}").text):
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;", page)

    # ------------------------------------------------------------- editing and review

    def test_edit_saves_content_and_keeps_the_tracking_token(self):
        eid = self.email()
        token = self.row(eid)["tracking_token"]
        self.follow(self.post(f"/emails/{eid}/save", subject="  New   subject ", body="Hi Ali,\r\n\r\nRewritten."))
        row = self.row(eid)
        self.assertEqual(row["subject"], "New subject")
        self.assertEqual(g.editable_body(row["body_text"]), "Hi Ali,\n\nRewritten.")
        self.assertIn(g.FOOTER_TEXT.splitlines()[0], row["body_text"])
        self.assertIn(f"{BASE}/{token}", row["body_html"])
        self.assertIn("Rewritten.", row["body_html"])
        self.assertIsNotNone(row["edited_at"])
        self.assertEqual(row["status"], "in_review")

    def test_editing_an_approved_email_sends_it_back_to_review(self):
        eid = self.email("approved")
        page = self.follow(self.post(f"/emails/{eid}/save", subject="Changed", body="Hi"))
        self.assertIn("went back to review", page.text)
        self.assertEqual((self.row(eid)["status"], self.row(eid)["reviewed_at"]), ("in_review", None))

    def test_sent_emails_cannot_be_edited(self):
        eid = self.email("sent")
        self.post(f"/emails/{eid}/save", subject="Changed", body="Hi")
        self.assertEqual(self.row(eid)["subject"], "Hello there")
        self.assertIn("readonly", self.client.get(f"/emails/{eid}").text)

    def test_empty_or_oversized_edits_are_refused(self):
        eid = self.email()
        for subject, body in (("", "Hi"), ("S", "   "), ("x" * 300, "Hi"), ("S", "b" * 6000)):
            self.post(f"/emails/{eid}/save", subject=subject, body=body)
        self.assertEqual(self.row(eid)["subject"], "Hello there")

    def test_approve_moves_to_the_next_email_in_review(self):
        first, second = self.email(), self.email()
        response = self.post(f"/emails/{first}/approve")
        self.assertTrue(response.headers["location"].startswith(f"/emails/{second}"))
        row = self.row(first)
        self.assertEqual(row["status"], "approved")
        self.assertIsNotNone(row["reviewed_at"])

    def test_placeholder_text_blocks_approval(self):
        eid = self.email(body="Hi PLACEHOLDER NAME")
        self.post(f"/emails/{eid}/approve")
        self.assertEqual(self.row(eid)["status"], "in_review")

    def test_reject_with_note_and_reopen(self):
        eid = self.email()
        self.post(f"/emails/{eid}/reject", note="Too salesy")
        self.assertEqual((self.row(eid)["status"], self.row(eid)["review_note"]), ("rejected", "Too salesy"))
        self.assertIn("Too salesy", self.client.get(f"/emails/{eid}").text)
        self.post(f"/emails/{eid}/reopen")
        self.assertEqual(self.row(eid)["status"], "in_review")

    def test_wrong_state_transitions_are_refused(self):
        sent = self.email("sent")
        for action in ("approve", "reject", "reopen", "regenerate"):
            self.post(f"/emails/{sent}/{action}")
        self.assertEqual(self.row(sent)["status"], "sent")

    def test_bulk_approve_and_reject(self):
        a, b, c = self.email(), self.email(body="PLACEHOLDER"), self.email()
        page = self.follow(self.post("/emails/bulk", action="approve", ids=[a, b], status="in_review"))
        self.assertIn("1 email(s) approved, 1 skipped", page.text)
        self.assertEqual([self.row(i)["status"] for i in (a, b, c)], ["approved", "in_review", "in_review"])
        self.post("/emails/bulk", action="reject", ids=[b, c], status="in_review")
        self.assertEqual([self.row(i)["status"] for i in (b, c)], ["rejected", "rejected"])

    def test_regenerate_rewrites_with_the_llm(self):
        eid = self.email("rejected")
        token = self.row(eid)["tracking_token"]
        page = self.follow(self.post(f"/emails/{eid}/regenerate"))
        self.assertIn("Regenerated with openai", page.text)
        row = self.row(eid)
        self.assertEqual(row["status"], "in_review")
        self.assertIn("Idea for Clinic", row["subject"])
        self.assertEqual(row["tracking_token"], token)
        self.assertIn(f"{BASE}/{token}", row["body_html"])

    def test_regenerate_failure_is_shown_and_changes_nothing(self):
        eid = self.email()
        self.llm.mode = "bad"
        page = self.follow(self.post(f"/emails/{eid}/regenerate"))
        self.assertIn("Regeneration failed", page.text)
        self.assertEqual(self.row(eid)["subject"], "Hello there")

    # ------------------------------------------------------------- sending

    def test_send_needs_the_typed_recipient_and_then_sends(self):
        eid = self.email("approved")
        recipient = self.row(eid)["recipient"]
        self.follow(self.post(f"/emails/{eid}/send", confirm="wrong@x.org"))
        self.assertEqual((self.row(eid)["status"], self.smtp.messages), ("approved", []))
        page = self.follow(self.post(f"/emails/{eid}/send", confirm=recipient.upper()))
        self.assertIn(f"Sent to {recipient}", page.text)
        self.assertEqual(self.row(eid)["status"], "sent")
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(self.smtp.messages[0][1], [recipient])
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("select outreach_status from prospects")).scalar(), "contacted")

    def test_only_approved_emails_can_be_sent(self):
        eid = self.email("in_review")
        self.post(f"/emails/{eid}/send", confirm=self.row(eid)["recipient"])
        self.assertEqual((self.row(eid)["status"], self.smtp.messages), ("in_review", []))

    def test_do_not_contact_prospect_is_never_sent(self):
        eid = self.email("approved", do_not_contact=True)
        page = self.follow(self.post(f"/emails/{eid}/send", confirm=self.row(eid)["recipient"]))
        self.assertIn("no longer ready", page.text)
        self.assertEqual(self.smtp.messages, [])

    def test_missing_smtp_settings(self):
        eid = self.email("approved")
        with mock.patch.dict(os.environ, {"SMTP_PASSWORD": ""}):
            detail = self.client.get(f"/emails/{eid}").text
            self.assertIn("SMTP is not configured", detail)
            page = self.follow(self.post(f"/emails/{eid}/send", confirm=self.row(eid)["recipient"]))
        self.assertIn("SMTP is not configured", page.text)
        self.assertEqual(self.row(eid)["status"], "approved")

    def test_smtp_rejection_marks_failed(self):
        eid = self.email("approved", recipient="reject@x.org")
        page = self.follow(self.post(f"/emails/{eid}/send", confirm="reject@x.org"))
        self.assertIn("marked failed", page.text)
        self.assertEqual(self.row(eid)["status"], "failed")

    def test_opens_are_shown(self):
        eid = self.email("approved")
        self.post(f"/emails/{eid}/send", confirm=self.row(eid)["recipient"])
        with self.engine.begin() as c:
            c.execute(text("select record_email_open(tracking_token, 'Apple Mail') from emails where id=:i"), {"i": eid})
        page = self.client.get(f"/emails/{eid}").text
        self.assertIn("Apple Mail", page)
        self.assertIn("Opened", page)
        self.assertIn("100%", self.client.get("/emails?status=all").text)  # open rate

    # ------------------------------------------------------------- security

    def test_posts_without_the_form_token_are_refused(self):
        eid = self.email()
        for token in ("", "forged"):
            response = self.client.post(f"/emails/{eid}/approve", data={"csrf": token}, follow_redirects=False)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.row(eid)["status"], "in_review")

    def test_foreign_host_header_is_refused(self):
        response = self.client.get("/emails", headers={"host": "evil.example.com"})
        self.assertEqual(response.status_code, 403)

    def test_no_api_docs_exposed(self):
        for path in ("/docs", "/openapi.json", "/redoc"):
            self.assertEqual(self.client.get(path).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()
