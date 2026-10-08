"""End-to-end: the verify_emails command against the test Postgres and a fake Reacher server."""
import csv
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from pipeline.models import BusinessContact, Prospect
from pipeline.services import verification as v
from pipeline.test_runner import free_port
from tests.fakes import FakeReacher
from tests.support import make_business, make_campaign, make_contact, run_command


def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class VerifyCase:
    def setUp(self):
        self.fake = FakeReacher()
        self.addCleanup(self.fake.close)
        campaign = make_campaign()
        self.b = [None] + [make_business(campaign, f"Business {i}") for i in range(1, 9)]
        from pipeline.models import BusinessWebsiteProfile
        BusinessWebsiteProfile.objects.create(business=self.b[1], status="completed", qualification_score=78)
        BusinessWebsiteProfile.objects.create(business=self.b[2], status="completed", qualification_score=40)

    def contact(self, business_index, email, candidates=(), primary=True):
        return make_contact(self.b[business_index], email, candidates, primary=primary).pk

    def rows(self, **where):
        return list(Prospect.objects.filter(**where).order_by("id").values())

    def by_email(self):
        return {r["email"]: r for r in self.rows()}

    def run_cli(self, *args, reacher_url=None, expect=None, env=None):
        env = {"REACHER_URL": reacher_url or self.fake.url, "VERIFY_MAIL_FROM": "", "VERIFY_HELO": "", **(env or {})}
        result = run_command("verify_emails", "--no-auto-start", "--delay", "0", *args, env=env)
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def contact_row(self, cid):
        return BusinessContact.objects.filter(pk=cid).values().get()

    def checks(self, cid):
        return {c["email"]: c.get("check") for c in self.contact_row(cid)["candidate_emails"]}


class EndToEndTests(VerifyCase, TestCase):
    def test_every_address_is_stored_with_its_verdict_and_only_deliverable_is_ready(self):
        cid = self.contact(1, "good@a.org", ["bad@b.org", "catchall@c.org", "full@d.org", "blocked@e.org", "good2@f.org"])
        out = self.run_cli(expect=0).stdout
        rows = self.by_email()
        expected = {
            "good@a.org": ("deliverable", "deliverable", "ready"),
            "good2@f.org": ("deliverable", "deliverable", "ready"),
            "bad@b.org": ("undeliverable", "mailbox_not_found", "rejected"),
            "catchall@c.org": ("risky", "catch_all", "needs_review"),
            "full@d.org": ("risky", "full_inbox", "needs_review"),
            "blocked@e.org": ("unknown", "blocked", "needs_review"),
        }
        self.assertEqual({email: (r["email_status"], r["verdict"], r["outreach_status"]) for email, r in rows.items()}, expected)
        for r in rows.values():
            self.assertEqual((r["business_id"], r["contact_id"]), (self.b[1].pk, cid))
            self.assertEqual(r["email_verification_provider"], "reacher")
            self.assertIsNotNone(r["email_verified_at"])
            self.assertEqual(r["qualification_score"], 78)
            self.assertFalse(r["do_not_contact"])
            self.assertEqual(r["outreach_facts"], [])
            self.assertIsNone(r["last_contacted_at"])
            self.assertTrue(r["verification_details"]["probed"])
        self.assertTrue(rows["catchall@c.org"]["verification_details"]["is_catch_all"])
        self.assertIn("catch-all", rows["catchall@c.org"]["verification_note"])
        self.assertIsNone(rows["good@a.org"]["verification_note"])
        self.assertIn("6 saved to prospects; 2 deliverable (ready to email)", out)
        self.assertIn("[risky: catch_all] <catchall@c.org>", out)
        self.assertIn("3 address(es) need review", out)  # catch-all and full (risky) plus blocked (unknown)

    def test_every_outcome_is_recorded_on_the_contact(self):
        cid = self.contact(1, "good@a.org", ["bad@b.org", "catchall@c.org", "full@d.org", "blocked@e.org"])
        self.run_cli(expect=0)
        self.assertEqual(self.checks(cid), {"bad@b.org": "invalid", "catchall@c.org": "risky",
                                            "full@d.org": "risky", "blocked@e.org": "unknown"})
        row = self.contact_row(cid)
        self.assertEqual(row["email_status"], "deliverable")  # good@a.org is the contact's own email
        self.assertIsNotNone(row["email_checked_at"])
        self.assertEqual(row["email"], "good@a.org")  # never rewritten
        self.assertTrue(all(c.get("checked_at") for c in row["candidate_emails"]))

    def test_contact_email_is_checked_even_if_not_in_candidates(self):
        cid = self.contact(1, "good@a.org")
        BusinessContact.objects.filter(pk=cid).update(candidate_emails=[{"email": "good2@b.org"}])
        self.run_cli(expect=0)
        self.assertEqual(set(self.by_email()), {"good@a.org", "good2@b.org"})

    def test_missing_score_is_stored_as_null(self):
        self.contact(3, "good@a.org")  # business 3 has no profile
        self.run_cli(expect=0)
        self.assertIsNone(self.rows()[0]["qualification_score"])

    def test_rerun_skips_decided_addresses_and_rechecks_unknown(self):
        self.contact(1, "good@a.org", ["bad@b.org", "blocked@c.org"])
        self.run_cli(expect=0)
        self.assertEqual(sorted(self.fake.calls), ["bad@b.org", "blocked@c.org", "good@a.org"])
        self.fake.calls.clear()
        out = self.run_cli(expect=0).stdout
        self.assertEqual(self.fake.calls, ["blocked@c.org"])  # only the unknown one
        self.assertIn("2 skipped", out)
        self.assertEqual(len(self.rows()), 3)  # still one row per address

    def test_addresses_checked_before_verdicts_were_stored_are_checked_once_more(self):
        cid = self.contact(1, "good@a.org", ["bad@b.org"])
        BusinessContact.objects.filter(pk=cid).update(  # an old run recorded the verdict only on the contact
            candidate_emails=[{"email": "bad@b.org", "check": "invalid"}], email_status="deliverable")
        self.run_cli(expect=0)
        self.assertEqual(sorted(self.fake.calls), ["bad@b.org", "good@a.org"])
        self.assertEqual(self.by_email()["bad@b.org"]["verdict"], "mailbox_not_found")

    def test_unknown_becomes_ready_when_a_rerun_proves_it(self):
        cid = self.contact(1, "grey@a.org")
        self.run_cli(expect=0)
        row = self.rows()[0]
        self.assertEqual((row["email_status"], row["verdict"], row["outreach_status"]), ("unknown", "temporary_failure", "needs_review"))
        self.assertEqual(self.contact_row(cid)["email_status"], "unknown")
        self.fake.sequence["grey"] = ["good"]
        self.run_cli(expect=0)
        self.assertEqual([(r["email"], r["email_status"], r["verdict"], r["outreach_status"]) for r in self.rows()],
                         [("grey@a.org", "deliverable", "deliverable", "ready")])

    def test_recheck_checks_everything_again(self):
        self.contact(1, "good@a.org", ["bad@b.org"])
        self.run_cli(expect=0)
        self.fake.calls.clear()
        self.run_cli("--recheck", expect=0)
        self.assertEqual(sorted(self.fake.calls), ["bad@b.org", "good@a.org"])

    def test_a_prospect_that_stops_being_deliverable_is_updated_not_left_ready(self):
        self.contact(1, "good@a.org")
        self.run_cli(expect=0)
        self.assertEqual(self.rows()[0]["outreach_status"], "ready")
        self.fake.sequence["good"] = ["bad"]
        self.run_cli("--recheck", expect=0)
        row = self.rows()[0]
        self.assertEqual((row["email_status"], row["outreach_status"]), ("undeliverable", "rejected"))
        self.assertEqual(len(self.rows()), 1)

    def test_bad_addresses_are_stored_but_never_ready(self):
        self.contact(1, "bad@a.org", ["bad2@b.org"])
        self.run_cli(expect=0)
        self.run_cli("--recheck", expect=0)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({(r["email_status"], r["outreach_status"]) for r in rows}, {("undeliverable", "rejected")})

    def test_reruns_never_overwrite_downstream_columns(self):
        self.contact(1, "good@a.org", ["good2@b.org", "good@c.org", "good@d.org"])
        self.run_cli(expect=0)
        self.assertEqual(len(self.rows()), 4)
        from django.utils import timezone
        Prospect.objects.filter(email="good@a.org").update(outreach_priority="high", research_summary="nice",
                                                           outreach_facts=["f1"], last_contacted_at=timezone.now())
        Prospect.objects.filter(email="good2@b.org").update(do_not_contact=True)
        Prospect.objects.filter(email="good@c.org").update(outreach_status="contacted")
        self.fake.sequence["good2"] = ["bad"]  # only this one stops being deliverable on the recheck
        self.run_cli("--recheck", expect=0)
        rows = self.by_email()
        first = rows["good@a.org"]  # still deliverable: verification refreshed, later-stage data kept
        self.assertEqual(first["email_status"], "deliverable")
        self.assertEqual((first["outreach_priority"], first["research_summary"], first["outreach_facts"]), ("high", "nice", ["f1"]))
        self.assertIsNotNone(first["last_contacted_at"])
        dnc = rows["good2@b.org"]  # do_not_contact: verification updates, outreach_status and the flag stay
        self.assertEqual(dnc["email_status"], "undeliverable")
        self.assertTrue(dnc["do_not_contact"])
        self.assertEqual(dnc["outreach_status"], "ready")
        self.assertEqual(rows["good@c.org"]["outreach_status"], "contacted")  # moved on by a later stage
        self.assertEqual(rows["good@d.org"]["outreach_status"], "ready")

    def test_do_not_contact_prospect_is_never_made_ready(self):
        cid = self.contact(1, "good@a.org")
        Prospect.objects.create(business=self.b[1], contact_id=cid, email="good@a.org", do_not_contact=True)
        self.run_cli("--recheck", expect=0)
        row = self.rows()[0]
        self.assertEqual((row["email_status"], row["outreach_status"], row["do_not_contact"]), ("deliverable", "pending", True))

    def test_catch_all_domain_is_checked_once_and_nothing_is_ready(self):
        cid = self.contact(1, "catchall@c.org", ["bad@c.org", "bad2@c.org", "good@c.org"])
        out = self.run_cli(expect=0).stdout
        self.assertEqual(self.fake.calls, ["catchall@c.org"])
        rows = self.by_email()
        self.assertEqual(len(rows), 4)
        self.assertEqual({(r["email_status"], r["verdict"], r["outreach_status"]) for r in rows.values()},
                         {("risky", "catch_all", "needs_review")})
        self.assertTrue(rows["catchall@c.org"]["verification_details"]["probed"])
        self.assertEqual(rows["good@c.org"]["verification_details"], {"probed": False})  # not checked: same domain
        self.assertIn("not checked", rows["good@c.org"]["verification_note"])
        self.assertEqual(set(self.checks(cid).values()), {"risky"})
        self.assertIn("not checked", out)

    def test_probe_all_checks_every_address(self):
        cid = self.contact(1, "catchall@c.org", ["bad@c.org", "bad2@c.org", "good@c.org"])
        self.run_cli("--probe-all", expect=0)
        self.assertEqual(len(self.fake.calls), 4)
        self.assertEqual({e for e, r in self.by_email().items() if r["outreach_status"] == "ready"}, {"good@c.org"})
        self.assertEqual(self.checks(cid), {"bad@c.org": "invalid", "bad2@c.org": "invalid", "good@c.org": "safe"})

    def test_unreachable_domain_is_checked_once(self):
        cid = self.contact(1, "nosmtp@d.org", ["good@d.org", "good2@d.org"])
        self.run_cli(expect=0)
        self.assertEqual(self.fake.calls, ["nosmtp@d.org"])
        self.assertEqual({(r["email_status"], r["verdict"]) for r in self.rows()}, {("unknown", "smtp_unreachable")})
        self.assertFalse(Prospect.objects.filter(outreach_status="ready").exists())
        self.assertEqual(set(self.checks(cid).values()), {"unknown"})

    def test_greylisting_does_not_trigger_the_domain_shortcut(self):
        self.contact(1, "grey@d.org", ["good@d.org"])
        self.run_cli(expect=0)
        self.assertEqual(self.fake.calls, ["grey@d.org", "good@d.org"])
        self.assertEqual({e: r["verdict"] for e, r in self.by_email().items()},
                         {"grey@d.org": "temporary_failure", "good@d.org": "deliverable"})

    def test_port_25_blocked_streak_stops_the_run_keeping_saved_results(self):
        cids = [self.contact(i, f"nosmtp@d{i}.org") for i in range(1, 6)]
        result = self.run_cli(expect=1)
        self.assertIn("port 25 is", result.stdout)
        done = [self.contact_row(c)["email_checked_at"] is not None for c in cids]
        self.assertEqual(done.count(True), 3)

    def test_dry_run_writes_nothing(self):
        cid = self.contact(1, "good@a.org", ["bad@b.org"])
        out = self.run_cli("--dry-run", expect=0).stdout
        self.assertIn("[deliverable: deliverable]", out)
        self.assertIn("nothing saved (dry run)", out)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.checks(cid), {"bad@b.org": None})
        self.assertIsNone(self.contact_row(cid)["email_checked_at"])

    def test_filters(self):
        self.contact(1, "good@a.org", primary=True)
        second = self.contact(1, "good2@a.org", primary=False)
        self.contact(2, "good@b.org", primary=True)
        self.run_cli("--primary-only", expect=0)
        self.assertEqual(set(self.by_email()), {"good@a.org", "good@b.org"})
        Prospect.objects.all().delete()
        self.run_cli("--business-id", self.b[2].pk, "--recheck", expect=0)
        self.assertEqual(set(self.by_email()), {"good@b.org"})
        Prospect.objects.all().delete()
        self.run_cli("--min-score", "50", "--recheck", expect=0)
        self.assertEqual({r["business_id"] for r in self.rows()}, {self.b[1].pk})
        Prospect.objects.all().delete()
        self.run_cli("--limit", "1", "--recheck", expect=0)
        self.assertEqual(len({r["contact_id"] for r in self.rows()}), 1)
        Prospect.objects.all().delete()
        self.run_cli("--contact-id", second, "--recheck", expect=0)
        self.assertEqual(set(self.by_email()), {"good2@a.org"})

    def test_several_businesses_in_one_run(self):
        self.contact(1, "good@a.org")
        self.contact(2, "good@b.org")
        self.contact(3, "good@c.org")
        self.run_cli("--business-id", self.b[1].pk, "--business-id", self.b[3].pk, expect=0)
        self.assertEqual(set(self.by_email()), {"good@a.org", "good@c.org"})

    def test_max_candidates_caps_per_contact(self):
        self.contact(1, "good@a.org", ["good2@b.org", "good@c.org", "good@d.org"])
        self.run_cli("--max-candidates", "2", expect=0)
        self.assertEqual(len(self.rows()), 2)

    def test_report_csv_has_reasons_and_probe_flag(self):
        self.contact(1, "catchall@c.org", ["bad@c.org"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "r.csv"
            self.run_cli("--report", str(path), expect=0)
            with open(path) as handle:
                rows = {r["email"]: r for r in csv.DictReader(handle)}
        self.assertEqual(rows["catchall@c.org"]["probed"], "True")
        self.assertEqual(rows["catchall@c.org"]["catch_all"], "True")
        self.assertEqual(rows["bad@c.org"]["probed"], "False")
        self.assertIn("catch-all", rows["bad@c.org"]["note"])

    def test_reacher_down_exits_cleanly_without_writing(self):
        cid = self.contact(1, "good@a.org")
        result = self.run_cli(reacher_url="http://127.0.0.1:9", expect=1)
        self.assertIn("Cannot reach Reacher", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.contact_row(cid)["email_checked_at"])

    def test_reacher_dying_mid_run_keeps_earlier_results(self):
        self.contact(1, "good@a.org", ["dies@b.org", "good@c.org"])
        self.fake.shutdown_on = "dies"
        result = self.run_cli(expect=1)
        self.assertIn("Results so far are saved", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(set(self.by_email()), {"good@a.org"})

    def run_with_identity(self, mail_from, helo="mail.mine.org"):
        return run_command("verify_emails", "--no-auto-start", "--delay", "0", "--dry-run",
                           env={"REACHER_URL": self.fake.url, "VERIFY_MAIL_FROM": mail_from, "VERIFY_HELO": helo})

    def test_placeholder_sender_domain_is_refused(self):
        for sender in ("verify@example.org", "verify@host.invalid"):
            result = self.run_with_identity(sender)
            self.assertEqual(result.returncode, 1, sender)
            self.assertIn("reserved placeholder domain", result.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_sender_domain_that_cannot_receive_mail_stops_the_run(self):
        self.contact(1, "good@a.org")
        result = self.run_with_identity("nomx@nowhere.org")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Sender problem", result.stdout)
        self.assertEqual(self.fake.calls, ["nomx@nowhere.org"])  # only the sender was checked, no contact

    def test_working_sender_proceeds_to_contacts(self):
        self.contact(1, "good@a.org")
        result = self.run_with_identity("good@mine.org")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fake.calls, ["good@mine.org", "good@a.org"])
        self.assertIn("[deliverable: deliverable]", result.stdout)

    def test_remote_reacher_url_is_refused(self):
        result = self.run_cli(reacher_url="https://api.reacher.email", expect=1)
        self.assertIn("is not local", result.stdout)

    def test_invalid_helo_is_rejected_up_front(self):
        result = self.run_cli("--helo", "alpha", expect=1)
        self.assertIn("fully-qualified", result.stderr)

    def test_missing_helo_is_refused_when_managing_the_container(self):
        result = run_command("verify_emails", "--dry-run",
                             env={"REACHER_URL": self.fake.url, "VERIFY_HELO": "", "VERIFY_MAIL_FROM": ""})
        self.assertEqual(result.returncode, 1)
        self.assertIn("VERIFY_HELO is not set", result.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_bad_arguments(self):
        for args in (["--max-candidates", "-1"], ["--reacher-timeout", "0"], ["--delay", "-1"], ["--limit", "0"],
                     ["--limit", "x"]):
            self.assertEqual(self.run_cli(*args).returncode, 1, args)

    def test_no_contacts(self):
        self.assertIn("No contacts to process", self.run_cli(expect=0).stdout)

    def test_deleting_a_contact_removes_its_prospects(self):
        cid = self.contact(1, "good@a.org", ["good2@b.org"])
        self.run_cli(expect=0)
        self.assertEqual(len(self.rows()), 2)
        BusinessContact.objects.filter(pk=cid).delete()
        self.assertEqual(self.rows(), [])

    def test_database_enforces_one_row_per_contact_and_email(self):
        cid = self.contact(1, "good@a.org")
        Prospect.objects.create(business=self.b[1], contact_id=cid, email="x@a.org")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Prospect.objects.create(business=self.b[1], contact_id=cid, email="x@a.org")

    def test_database_defaults_for_plain_sql_inserts(self):
        cid = self.contact(1, "good@a.org")
        with connection.cursor() as cursor:
            cursor.execute("insert into prospects (business_id, contact_id, email) values (%s, %s, 'x@a.org')",
                           [self.b[1].pk, cid])
        row = self.rows()[0]
        self.assertEqual((row["outreach_status"], row["do_not_contact"], row["outreach_facts"]), ("pending", False, []))
        self.assertIsNotNone(row["created_at"])


class UpdatedAtTriggerTests(VerifyCase, TransactionTestCase):
    def test_updated_at_trigger(self):
        cid = self.contact(1, "good@a.org")
        Prospect.objects.create(business=self.b[1], contact_id=cid, email="x@a.org")
        before = self.rows()[0]["updated_at"]
        time.sleep(1.1)
        Prospect.objects.update(outreach_priority="low")  # a queryset update: only the database trigger sets it
        self.assertGreater(self.rows()[0]["updated_at"], before)

    def test_missing_table_gives_a_clear_message(self):
        self.contact(1, "good@a.org")
        with connection.cursor() as cursor:
            cursor.execute("alter table prospects rename to prospects_hidden")
        try:
            result = self.run_cli(expect=1)
            self.assertIn("python manage.py migrate", result.stdout)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("alter table prospects_hidden rename to prospects")


@unittest.skipUnless(docker_ok(), "Docker is not available")
class LiveReacherContainerTests(unittest.TestCase):
    """Starts the real reacherhq/backend image through ensure_reacher on a spare port."""

    def test_auto_start_configure_and_check(self):
        name = f"emailtest-reacher-{uuid.uuid4().hex[:8]}"
        port = free_port()
        client = v.ReacherClient(f"http://127.0.0.1:{port}", 60)
        self.addCleanup(lambda: subprocess.run(["docker", "rm", "-f", name], capture_output=True))
        version = v.ensure_reacher(client, "verify@example.org", "mail.example.org", True, container=name)
        self.assertRegex(version, r"^\d+\.\d+\.\d+")
        config = v.container_config(name)
        self.assertTrue(config["running"])
        self.assertEqual(config["env"]["RCH__FROM_EMAIL"], "verify@example.org")
        self.assertEqual(config["env"]["RCH__HELLO_NAME"], "mail.example.org")

        # A domain that cannot receive mail is answered without any SMTP traffic.
        nowhere = f"someone@nx-{uuid.uuid4().hex}.com"
        self.assertEqual(client.check(nowhere)["is_reachable"], "invalid")
        outcome = v.check_address(nowhere, client, 0, {})
        self.assertEqual(outcome["status"], "undeliverable")
        self.assertIn("no mail server", outcome["note"])

        # Already running with the right settings: nothing is restarted.
        started = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", name], capture_output=True, text=True).stdout
        self.assertEqual(v.ensure_reacher(client, "verify@example.org", "mail.example.org", True, container=name), version)
        again = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", name], capture_output=True, text=True).stdout
        self.assertEqual(started, again)

        # Changed identity: the container is rebuilt automatically with the new settings.
        v.ensure_reacher(client, "other@example.org", "mail2.example.org", True, container=name)
        config = v.container_config(name)
        self.assertEqual(config["env"]["RCH__FROM_EMAIL"], "other@example.org")
        self.assertEqual(config["env"]["RCH__HELLO_NAME"], "mail2.example.org")

        # A stopped container is started again, keeping its settings.
        subprocess.run(["docker", "stop", name], capture_output=True)
        v.ensure_reacher(client, "other@example.org", "mail2.example.org", True, container=name)
        self.assertTrue(v.container_config(name)["running"])
