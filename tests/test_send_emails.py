import os
import smtplib
import time
import uuid
from unittest import mock

from django.db import connection
from django.test import TestCase
import unittest

from pipeline.models import Email, Prospect
from pipeline.services import generation as g
from pipeline.services import sending as s
from tests.fake_smtp import FakeSmtpServer
from tests.support import make_business, make_campaign, make_contact, run_command

CONFIG = {"host": "h", "port": 465, "security": "ssl", "username": "u@axo.dev", "password": "p",
          "from_email": "u@axo.dev", "from_name": "Abdul Rauf", "reply_to": "", "timeout": 5}


class MessageTests(unittest.TestCase):
    EMAIL = {"recipient": "shoaib@b.pk", "subject": "Hello", "body_text": "Hi\n\n--\nFooter",
             "body_html": "<p>Hi</p><img src=\"https://t/abc\">"}

    def test_message_has_both_parts_and_headers(self):
        message = s.build_message(self.EMAIL, CONFIG)
        self.assertEqual(message["To"], "shoaib@b.pk")
        self.assertEqual(message["From"], "Abdul Rauf <u@axo.dev>")
        self.assertEqual(message["Subject"], "Hello")
        self.assertTrue(message["Message-ID"].endswith("@axo.dev>"))
        self.assertEqual(message["List-Unsubscribe"], "<mailto:u@axo.dev?subject=unsubscribe>")
        self.assertIsNone(message["Reply-To"])
        self.assertEqual(message.get_body(("plain",)).get_content().strip(), "Hi\n\n--\nFooter")
        self.assertIn("https://t/abc", message.get_body(("html",)).get_content())

    def test_reply_to_is_used_for_unsubscribe(self):
        message = s.build_message(self.EMAIL, {**CONFIG, "reply_to": "hello@axo.dev", "from_name": ""})
        self.assertEqual(message["Reply-To"], "hello@axo.dev")
        self.assertEqual(message["From"], "u@axo.dev")
        self.assertIn("hello@axo.dev", message["List-Unsubscribe"])

    def test_placeholder_detection(self):
        self.assertTrue(s.has_placeholder({**self.EMAIL, "body_text": "PLACEHOLDER ADDRESS"}))
        self.assertFalse(s.has_placeholder(self.EMAIL))

    def test_config_from_env(self):
        with mock.patch.dict(os.environ, {"SMTP_USERNAME": "a@b.org", "SMTP_PASSWORD": "x", "SMTP_PORT": "",
                                          "SMTP_HOST": "", "SMTP_SECURITY": "", "SMTP_FROM_EMAIL": ""}):
            config = s.smtp_config(5)
        self.assertEqual((config["host"], config["port"], config["security"], config["from_email"]),
                         ("smtp.hostinger.com", 465, "ssl", "a@b.org"))
        with mock.patch.dict(os.environ, {"SMTP_PORT": "587", "SMTP_SECURITY": ""}):
            self.assertEqual(s.smtp_config(5)["security"], "starttls")

    def test_config_problems(self):
        self.assertEqual(s.config_problems(CONFIG), [])
        problems = s.config_problems({**CONFIG, "username": "", "password": "", "from_email": "", "security": "tls"})
        self.assertEqual(len(problems), 4)
        self.assertIn("SMTP_FROM_NAME is still a placeholder", s.config_problems({**CONFIG, "from_name": "PLACEHOLDER Name"}))

    def test_error_classification(self):
        refused = smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"user unknown")})
        self.assertEqual(s.classify_send_error(refused)[0], "failed")
        self.assertEqual(s.classify_send_error(smtplib.SMTPRecipientsRefused({"x@y.z": (451, b"later")}))[0], "stop")
        self.assertEqual(s.classify_send_error(smtplib.SMTPAuthenticationError(535, b"bad"))[0], "stop")
        self.assertEqual(s.classify_send_error(smtplib.SMTPDataError(554, b"spam"))[0], "failed")
        self.assertEqual(s.classify_send_error(smtplib.SMTPDataError(451, b"later"))[0], "stop")
        self.assertEqual(s.classify_send_error(smtplib.SMTPSenderRefused(553, b"bad from", "u@x"))[0], "stop")
        self.assertEqual(s.classify_send_error(ConnectionRefusedError())[0], "stop")

    def test_generated_footer_is_in_text_and_html(self):
        text_version = g.render_text("Hi Ali")
        self.assertTrue(text_version.startswith("Hi Ali\n\n--\n"))
        self.assertIn(g.FOOTER_TEXT.splitlines()[0], text_version)
        html_version = g.render_html("Hi Ali", "https://t/tok")
        self.assertIn("unsubscribe", html_version)
        self.assertLess(html_version.index("<img"), html_version.index("unsubscribe"))  # logo, then the small print
        self.assertIn('<a href="https://axioware.tech">', html_version)
        self.assertFalse(g.FOOTER_IS_PLACEHOLDER)
        self.assertNotIn("PLACEHOLDER", g.FOOTER_TEXT)


SMTP_ENV_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL",
                 "SMTP_FROM_NAME", "SMTP_REPLY_TO")


class SendCase:
    def setUp(self):
        self.smtp = FakeSmtpServer()
        self.addCleanup(self.smtp.close)
        self.campaign = make_campaign()

    def smtp_env(self, **overrides):
        return {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.port), "SMTP_SECURITY": "none",
                "SMTP_USERNAME": "user@x.org", "SMTP_PASSWORD": "secret", "SMTP_FROM_EMAIL": "user@x.org",
                "SMTP_FROM_NAME": "Sender Name", "SMTP_REPLY_TO": "", **overrides}

    def draft(self, recipient, body="Hi there", subject="Hello", status="approved", **prospect_cols):
        cols = {"email_status": "deliverable", "outreach_status": "ready", "do_not_contact": False, **prospect_cols}
        business = make_business(self.campaign, f"B {recipient}")
        contact = make_contact(business, name="P")
        prospect = Prospect.objects.create(business=business, contact=contact, email=recipient, **cols)
        return Email.objects.create(prospect=prospect, business=business, contact=contact, recipient=recipient,
                                    subject=subject, body_text=body, body_html=f"<p>{body}</p>",
                                    tracking_token=uuid.uuid4().hex + uuid.uuid4().hex, status=status).pk

    def row(self, email_id):
        return Email.objects.filter(pk=email_id).values().get()

    def prospect_of(self, email_id):
        return Prospect.objects.filter(emails__id=email_id).values().get()


class SendEndToEndTests(SendCase, TestCase):
    def run_cli(self, *args, expect=None, **env_overrides):
        result = run_command("send_emails", "--delay", "0", "--allow-placeholders", *args, env=self.smtp_env(**env_overrides))
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def test_preview_sends_and_changes_nothing(self):
        eid = self.draft("a@b.org")
        out = self.run_cli(expect=0).stdout
        self.assertIn("Preview", out)
        self.assertEqual(self.smtp.messages, [])
        self.assertEqual(self.row(eid)["status"], "approved")

    def test_send_marks_sent_and_updates_the_prospect(self):
        eid = self.draft("a@b.org", body="Hi Ali")
        self.run_cli("--send", expect=0)
        self.assertEqual(len(self.smtp.messages), 1)
        mail_from, rcpts, message = self.smtp.messages[0]
        self.assertEqual((mail_from, rcpts), ("user@x.org", ["a@b.org"]))
        self.assertEqual(message["Subject"], "Hello")
        self.assertEqual(message["From"], "Sender Name <user@x.org>")
        self.assertIn("Hi Ali", message.get_body(("plain",)).get_content())
        row = self.row(eid)
        self.assertEqual((row["status"], row["sent_from"], row["send_error"]), ("sent", "user@x.org", None))
        self.assertIsNotNone(row["sent_at"])
        self.assertEqual(row["message_id"], message["Message-ID"])
        prospect = self.prospect_of(eid)
        self.assertEqual(prospect["outreach_status"], "contacted")
        self.assertIsNotNone(prospect["last_contacted_at"])

    def test_sent_email_then_counts_opens(self):
        eid = self.draft("a@b.org")
        self.run_cli("--send", expect=0)
        with connection.cursor() as cursor:
            cursor.execute("select record_email_open(tracking_token, 'Mail') from emails where id=%s", [eid])
            self.assertTrue(cursor.fetchone()[0])
        self.assertEqual((self.row(eid)["status"], self.row(eid)["open_count"]), ("opened", 1))

    def test_a_rerun_never_sends_twice(self):
        self.draft("a@b.org")
        self.run_cli("--send", expect=0)
        self.assertIn("No approved emails ready", self.run_cli("--send", expect=0).stdout)
        self.assertEqual(len(self.smtp.messages), 1)

    def test_ineligible_prospects_are_never_emailed(self):
        ids = [self.draft("a@b.org", do_not_contact=True), self.draft("b@b.org", outreach_status="needs_review"),
               self.draft("c@b.org", email_status="undeliverable")]
        self.assertIn("No approved emails ready", self.run_cli("--send", expect=0).stdout)
        self.assertEqual(self.smtp.messages, [])
        self.assertEqual({self.row(i)["status"] for i in ids}, {"approved"})

    def test_only_approved_emails_are_ever_sent(self):
        ids = {status: self.draft(f"{status}@b.org", status=status) for status in ("in_review", "rejected", "failed")}
        self.assertIn("No approved emails ready", self.run_cli("--send", expect=0).stdout)
        self.assertEqual(self.smtp.messages, [])
        self.assertEqual({st: self.row(i)["status"] for st, i in ids.items()},
                         {"in_review": "in_review", "rejected": "rejected", "failed": "failed"})

    def test_recipient_must_still_match_the_prospect(self):
        eid = self.draft("a@b.org")
        Prospect.objects.update(email="changed@b.org")
        self.assertIn("No approved emails ready", self.run_cli("--send", expect=0).stdout)
        self.assertEqual(self.row(eid)["status"], "approved")

    def test_rejected_recipient_is_failed_and_others_still_send(self):
        bad = self.draft("reject@b.org")
        good = self.draft("ok@b.org")
        out = self.run_cli("--send", expect=0).stdout
        self.assertEqual(self.row(bad)["status"], "failed")
        self.assertIn("550", self.row(bad)["send_error"])
        self.assertEqual(self.row(good)["status"], "sent")
        self.assertIn("1 sent, 1 failed", out)
        self.assertEqual(self.prospect_of(bad)["outreach_status"], "ready")

    def test_content_rejected_as_spam_is_failed(self):
        self.smtp.reject_data_for.add("spammy")
        eid = self.draft("spammy@b.org")
        self.run_cli("--send", expect=0)
        self.assertEqual(self.row(eid)["status"], "failed")
        self.assertIn("554", self.row(eid)["send_error"])

    def test_temporary_failure_stops_and_returns_to_approved(self):
        first = self.draft("tempfail@b.org")
        second = self.draft("ok@b.org")
        result = self.run_cli("--send", expect=1)
        self.assertIn("Stopping", result.stdout)
        self.assertEqual(self.row(first)["status"], "approved")
        self.assertIn("451", self.row(first)["send_error"])
        self.assertEqual(self.row(second)["status"], "approved")  # not attempted
        self.assertEqual(self.smtp.messages, [])

    def test_wrong_password_stops_without_sending(self):
        eid = self.draft("a@b.org")
        result = self.run_cli("--send", expect=1, SMTP_PASSWORD="wrong")
        self.assertIn("login failed", result.stdout)
        self.assertEqual(self.row(eid)["status"], "approved")
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_unreachable_server_stops_cleanly(self):
        eid = self.draft("a@b.org")
        result = self.run_cli("--send", expect=1, SMTP_PORT="9")
        self.assertEqual(self.row(eid)["status"], "approved")
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_server_hanging_up_mid_send_returns_to_approved(self):
        self.smtp.drop_on.add("drop")
        eid = self.draft("drop@b.org")
        result = self.run_cli("--send", expect=1)
        self.assertEqual(self.row(eid)["status"], "approved")
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_email_stuck_in_sending_is_reported_and_not_resent(self):
        eid = self.draft("a@b.org")
        Email.objects.update(status="sending")
        out = self.run_cli("--send", expect=0).stdout
        self.assertIn("stuck in 'sending'", out)
        self.assertEqual(self.smtp.messages, [])
        self.assertEqual(self.row(eid)["status"], "sending")

    def test_placeholder_content_is_refused_without_the_flag(self):
        eid = self.draft("a@b.org", body="PLACEHOLDER COMPANY ADDRESS")
        env = self.smtp_env()
        result = run_command("send_emails", "--send", "--delay", "0", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)  # prompt/footer are real now...
        self.assertIn("contains placeholder text", result.stdout)  # ...but this draft still has placeholder text
        preview = run_command("send_emails", "--delay", "0", env=env)
        self.assertIn("contains placeholder text", preview.stdout)
        self.assertEqual(self.row(eid)["status"], "approved")
        self.assertEqual(self.smtp.messages, [])

    def test_missing_smtp_settings_are_refused(self):
        self.draft("a@b.org")
        result = self.run_cli("--send", SMTP_USERNAME="", SMTP_PASSWORD="", SMTP_FROM_EMAIL="")
        self.assertEqual(result.returncode, 1)
        for name in ("SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL"):
            self.assertIn(name, result.stderr)

    def test_filters(self):
        first = self.draft("a@b.org")
        second = self.draft("b@b.org")
        third = self.draft("c@b.org")
        self.run_cli("--send", "--email-id", second, expect=0)
        self.assertEqual([self.row(i)["status"] for i in (first, second, third)], ["approved", "sent", "approved"])
        self.run_cli("--send", "--limit", "1", expect=0)
        self.assertEqual([self.row(i)["status"] for i in (first, third)], ["sent", "approved"])

    def test_delay_between_sends(self):
        self.draft("a@b.org")
        self.draft("b@b.org")
        started = time.monotonic()
        result = run_command("send_emails", "--send", "--delay", "1.5", "--allow-placeholders",
                             env=self.smtp_env(SMTP_FROM_NAME=""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertEqual(len(self.smtp.messages), 2)
