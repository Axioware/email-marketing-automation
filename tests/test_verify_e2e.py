"""End-to-end: the real CLI (subprocess) against a throwaway local Postgres built with the real migrations.

Needs Docker (skipped otherwise). Never touches the DATABASE_URL in .env: the subprocess gets its own.
"""
import csv
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

from tests.fakes import FakeReacher

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_contact_emails.py"


def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]




@unittest.skipUnless(docker_ok(), "Docker is not available")
class EndToEndTests(unittest.TestCase):
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
        else:
            raise RuntimeError("test Postgres did not start")
        migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                                  env={**os.environ, "DATABASE_URL": cls.url}, capture_output=True, text=True)
        if migrated.returncode != 0:
            raise RuntimeError(migrated.stderr)
        cls.engine = create_engine(cls.url)
        with cls.engine.begin() as conn:
            conn.execute(text("insert into discovery_campaigns (id, name) values (1, 'test')"))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)

    def setUp(self):
        self.fake = FakeReacher()
        self.addCleanup(self.fake.close)
        with self.engine.begin() as conn:
            conn.execute(text("truncate prospects, business_contacts, business_website_profiles, businesses restart identity cascade"))
            for i in range(1, 9):
                conn.execute(text("insert into businesses (id, discovery_campaign_id, name) values (:i, 1, :n)"), {"i": i, "n": f"Business {i}"})
            conn.execute(text("insert into business_website_profiles (business_id, status, qualification_score) values (1, 'completed', 78), (2, 'completed', 40)"))

    # ---------------------------------------------------------------- helpers

    def contact(self, business_id, email, candidates=(), primary=True):
        import json
        with self.engine.begin() as conn:
            return conn.execute(text(
                """insert into business_contacts (business_id, name, email, email_source, is_primary, candidate_emails)
                   values (:b, :n, :e, 'inferred', :p, cast(:c as jsonb)) returning id"""),
                {"b": business_id, "n": f"Person {business_id}", "e": email, "p": primary,
                 "c": json.dumps([{"email": c, "pattern": "x", "confidence": 0.1} for c in candidates])}).scalar_one()

    def rows(self, **where):
        clause = " and ".join(f"{k} = :{k}" for k in where) or "true"
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(f"select * from prospects where {clause} order by id"), where)]

    def by_email(self):
        return {r["email"]: r for r in self.rows()}

    def run_cli(self, *args, reacher_url=None, expect=None):
        env = {**os.environ, "DATABASE_URL": self.url, "REACHER_URL": reacher_url or self.fake.url,
               "VERIFY_MAIL_FROM": "", "VERIFY_HELO": ""}
        result = subprocess.run([sys.executable, str(SCRIPT), "--no-auto-start", "--delay", "0", *args],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    # ---------------------------------------------------------------- tests

    def contact_row(self, cid):
        with self.engine.connect() as conn:
            return dict(conn.execute(text("select * from business_contacts where id=:i"), {"i": cid}).one()._mapping)

    def checks(self, cid):
        return {c["email"]: c.get("check") for c in self.contact_row(cid)["candidate_emails"]}

    def test_only_deliverable_addresses_become_prospects(self):
        cid = self.contact(1, "good@a.org", ["bad@b.org", "catchall@c.org", "full@d.org", "blocked@e.org", "good2@f.org"])
        out = self.run_cli(expect=0).stdout
        rows = self.by_email()
        self.assertEqual(set(rows), {"good@a.org", "good2@f.org"})  # nothing undeliverable/risky/unknown
        for r in rows.values():
            self.assertEqual((r["business_id"], r["contact_id"]), (1, cid))
            self.assertEqual((r["email_status"], r["outreach_status"]), ("deliverable", "ready"))
            self.assertEqual(r["email_verification_provider"], "reacher")
            self.assertIsNotNone(r["email_verified_at"])
            self.assertEqual(r["qualification_score"], 78)
            self.assertFalse(r["do_not_contact"])
            self.assertEqual(r["outreach_facts"], [])
            self.assertIsNone(r["outreach_priority"])
            self.assertIsNone(r["research_summary"])
            self.assertIsNone(r["last_contacted_at"])
        self.assertIn("2 added or updated as prospects", out)
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
        self.contact(1, "good@a.org")
        with self.engine.begin() as conn:
            conn.execute(text("update business_contacts set candidate_emails = cast('[{\"email\": \"good2@b.org\"}]' as jsonb)"))
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
        self.assertEqual(len(self.rows()), 1)

    def test_unknown_can_become_a_prospect_on_rerun(self):
        cid = self.contact(1, "grey@a.org")
        self.run_cli(expect=0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.contact_row(cid)["email_status"], "unknown")
        self.fake.sequence["grey"] = ["good"]
        self.run_cli(expect=0)
        self.assertEqual([(r["email"], r["email_status"]) for r in self.rows()], [("grey@a.org", "deliverable")])

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

    def test_recheck_never_creates_prospects_for_bad_addresses(self):
        self.contact(1, "bad@a.org", ["bad2@b.org"])
        self.run_cli(expect=0)
        self.run_cli("--recheck", expect=0)
        self.assertEqual(self.rows(), [])

    def test_reruns_never_overwrite_downstream_columns(self):
        self.contact(1, "good@a.org", ["good2@b.org", "good@c.org", "good@d.org"])
        self.run_cli(expect=0)
        self.assertEqual(len(self.rows()), 4)
        with self.engine.begin() as conn:
            conn.execute(text("""update prospects set outreach_priority='high', research_summary='nice', outreach_facts='["f1"]'::jsonb,
                                 last_contacted_at=now() where email='good@a.org'"""))
            conn.execute(text("update prospects set do_not_contact=true where email='good2@b.org'"))
            conn.execute(text("update prospects set outreach_status='contacted' where email='good@c.org'"))
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
        with self.engine.begin() as conn:
            conn.execute(text("insert into prospects (business_id, contact_id, email, do_not_contact) values (1, :c, 'good@a.org', true)"), {"c": cid})
        self.run_cli("--recheck", expect=0)
        row = self.rows()[0]
        self.assertEqual((row["email_status"], row["outreach_status"], row["do_not_contact"]), ("deliverable", "pending", True))

    def test_catch_all_domain_is_checked_once_and_nothing_becomes_a_prospect(self):
        cid = self.contact(1, "catchall@c.org", ["bad@c.org", "bad2@c.org", "good@c.org"])
        out = self.run_cli(expect=0).stdout
        self.assertEqual(self.fake.calls, ["catchall@c.org"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(set(self.checks(cid).values()), {"risky"})
        self.assertIn("not checked", out)

    def test_probe_all_checks_every_address(self):
        cid = self.contact(1, "catchall@c.org", ["bad@c.org", "bad2@c.org", "good@c.org"])
        self.run_cli("--probe-all", expect=0)
        self.assertEqual(len(self.fake.calls), 4)
        self.assertEqual(set(self.by_email()), {"good@c.org"})
        self.assertEqual(self.checks(cid), {"bad@c.org": "invalid", "bad2@c.org": "invalid", "good@c.org": "safe"})

    def test_unreachable_domain_is_checked_once(self):
        cid = self.contact(1, "nosmtp@d.org", ["good@d.org", "good2@d.org"])
        self.run_cli(expect=0)
        self.assertEqual(self.fake.calls, ["nosmtp@d.org"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(set(self.checks(cid).values()), {"unknown"})

    def test_greylisting_does_not_trigger_the_domain_shortcut(self):
        self.contact(1, "grey@d.org", ["good@d.org"])
        self.run_cli(expect=0)
        self.assertEqual(self.fake.calls, ["grey@d.org", "good@d.org"])
        self.assertEqual(set(self.by_email()), {"good@d.org"})

    def test_port_25_blocked_streak_stops_the_run_keeping_saved_results(self):
        cids = [self.contact(i, f"nosmtp@d{i}.org") for i in range(1, 6)]
        result = self.run_cli(expect=1)
        self.assertIn("port 25 is", result.stdout)
        done = [self.contact_row(c)["email_checked_at"] is not None for c in cids]
        self.assertEqual(done.count(True), 3)

    def test_dry_run_writes_nothing(self):
        cid = self.contact(1, "good@a.org", ["bad@b.org"])
        out = self.run_cli("--dry-run", expect=0).stdout
        self.assertIn("[deliverable]", out)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.checks(cid), {"bad@b.org": None})
        self.assertIsNone(self.contact_row(cid)["email_checked_at"])

    def test_filters(self):
        self.contact(1, "good@a.org", primary=True)
        self.contact(1, "good2@a.org", primary=False)
        self.contact(2, "good@b.org", primary=True)
        self.run_cli("--primary-only", expect=0)
        self.assertEqual(set(self.by_email()), {"good@a.org", "good@b.org"})
        with self.engine.begin() as conn:
            conn.execute(text("truncate prospects restart identity cascade"))
        self.run_cli("--business-id", "2", "--recheck", expect=0)
        self.assertEqual(set(self.by_email()), {"good@b.org"})
        with self.engine.begin() as conn:
            conn.execute(text("truncate prospects restart identity cascade"))
        self.run_cli("--min-score", "50", "--recheck", expect=0)
        self.assertEqual({r["business_id"] for r in self.rows()}, {1})
        with self.engine.begin() as conn:
            conn.execute(text("truncate prospects restart identity cascade"))
        self.run_cli("--limit", "1", "--recheck", expect=0)
        self.assertEqual(len({r["contact_id"] for r in self.rows()}), 1)

    def test_max_candidates_caps_per_contact(self):
        self.contact(1, "good@a.org", ["good2@b.org", "good@c.org", "good@d.org"])
        self.run_cli("--max-candidates", "2", expect=0)
        self.assertEqual(len(self.rows()), 2)

    def test_report_csv_has_reasons_and_probe_flag(self):
        self.contact(1, "catchall@c.org", ["bad@c.org"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "r.csv"
            self.run_cli("--report", str(path), expect=0)
            rows = {r["email"]: r for r in csv.DictReader(open(path))}
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

    def test_missing_table_gives_a_clear_message(self):
        self.contact(1, "good@a.org")
        with self.engine.begin() as conn:
            conn.execute(text("alter table prospects rename to prospects_hidden"))
        try:
            result = self.run_cli(expect=1)
            self.assertIn("alembic upgrade head", result.stdout)
        finally:
            with self.engine.begin() as conn:
                conn.execute(text("alter table prospects_hidden rename to prospects"))

    def run_with_identity(self, mail_from, helo="mail.mine.org"):
        env = {**os.environ, "DATABASE_URL": self.url, "REACHER_URL": self.fake.url,
               "VERIFY_MAIL_FROM": mail_from, "VERIFY_HELO": helo}
        return subprocess.run([sys.executable, str(SCRIPT), "--no-auto-start", "--delay", "0", "--dry-run"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    def test_placeholder_sender_domain_is_refused(self):
        for sender in ("verify@example.org", "verify@host.invalid"):
            result = self.run_with_identity(sender)
            self.assertEqual(result.returncode, 2, sender)
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
        self.assertIn("[deliverable]", result.stdout)

    def test_remote_reacher_url_is_refused(self):
        result = self.run_cli(reacher_url="https://api.reacher.email", expect=1)
        self.assertIn("is not local", result.stdout)

    def test_invalid_helo_is_rejected_up_front(self):
        result = self.run_cli("--helo", "alpha")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fully-qualified", result.stderr)

    def test_missing_helo_is_refused_when_managing_the_container(self):
        env = {**os.environ, "DATABASE_URL": self.url, "REACHER_URL": self.fake.url, "VERIFY_HELO": "", "VERIFY_MAIL_FROM": ""}
        result = subprocess.run([sys.executable, str(SCRIPT), "--dry-run"], cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("VERIFY_HELO is not set", result.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_bad_arguments(self):
        for args in (["--max-candidates", "-1"], ["--reacher-timeout", "0"], ["--delay", "-1"]):
            self.assertEqual(self.run_cli(*args).returncode, 2, args)

    def test_no_contacts(self):
        self.assertIn("No contacts to process", self.run_cli(expect=0).stdout)

    def test_deleting_a_contact_removes_its_prospects(self):
        cid = self.contact(1, "good@a.org", ["good2@b.org"])
        self.run_cli(expect=0)
        self.assertEqual(len(self.rows()), 2)
        with self.engine.begin() as conn:
            conn.execute(text("delete from business_contacts where id=:i"), {"i": cid})
        self.assertEqual(self.rows(), [])

    def test_database_enforces_one_row_per_contact_and_email(self):
        cid = self.contact(1, "good@a.org")
        insert = text("insert into prospects (business_id, contact_id, email) values (1, :c, 'x@a.org')")
        with self.engine.begin() as conn:
            conn.execute(insert, {"c": cid})
        with self.assertRaises(Exception):
            with self.engine.begin() as conn:
                conn.execute(insert, {"c": cid})

    def test_table_defaults_and_updated_at_trigger(self):
        cid = self.contact(1, "good@a.org")
        with self.engine.begin() as conn:
            conn.execute(text("insert into prospects (business_id, contact_id, email) values (1, :c, 'x@a.org')"), {"c": cid})
        row = self.rows()[0]
        self.assertEqual((row["outreach_status"], row["do_not_contact"], row["outreach_facts"]), ("pending", False, []))
        time.sleep(1.1)
        with self.engine.begin() as conn:
            conn.execute(text("update prospects set outreach_priority='low'"))
        self.assertGreater(self.rows()[0]["updated_at"], row["updated_at"])


@unittest.skipUnless(docker_ok(), "Docker is not available")
class LiveReacherContainerTests(unittest.TestCase):
    """Starts the real reacherhq/backend image through ensure_reacher on a spare port."""

    def test_auto_start_configure_and_check(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import verify_contact_emails as v

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


if __name__ == "__main__":
    unittest.main()
