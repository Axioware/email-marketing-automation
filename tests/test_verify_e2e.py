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
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", cls.name, "-e", "POSTGRES_PASSWORD=test",
             "-p", f"127.0.0.1:{cls.port}:5432", "postgres:16-alpine"],
            check=True, capture_output=True,
        )
        cls.url = f"postgresql+psycopg2://postgres:test@127.0.0.1:{cls.port}/postgres"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(["docker", "exec", cls.name, "pg_isready", "-U", "postgres"], capture_output=True)
            if ready.returncode == 0:
                try:
                    create_engine(cls.url).connect().close()
                    break
                except Exception:
                    pass
            time.sleep(1)
        else:
            raise RuntimeError("test Postgres did not start")
        env = {**os.environ, "DATABASE_URL": cls.url}
        migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                                  capture_output=True, text=True)
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
            conn.execute(text("truncate business_contacts, businesses restart identity cascade"))
            for i in range(1, 13):
                conn.execute(text("insert into businesses (id, discovery_campaign_id, name) values (:i, 1, :n)"),
                             {"i": i, "n": f"Business {i}"})

    def add(self, business_id, email, *, source="website", status="unverified", primary=True, candidates=()):
        with self.engine.begin() as conn:
            return conn.execute(
                text("""insert into business_contacts
                        (business_id, name, email, email_source, email_status, is_primary, candidate_emails)
                        values (:b, :n, :e, :s, :st, :p, cast(:c as jsonb)) returning id"""),
                {"b": business_id, "n": email.split("@")[0], "e": email, "s": source, "st": status, "p": primary,
                 "c": json.dumps([{"email": c, "pattern": "x", "confidence": 0.1} for c in candidates])},
            ).scalar_one()

    def row(self, contact_id):
        with self.engine.connect() as conn:
            return dict(conn.execute(text("select * from business_contacts where id=:i"), {"i": contact_id}).one()._mapping)

    def run_cli(self, *args, reacher_url=None, expect=None):
        env = {**os.environ, "DATABASE_URL": self.url, "REACHER_URL": reacher_url or self.fake.url}
        env["VERIFY_MAIL_FROM"] = env["VERIFY_HELO"] = ""  # empty values stop the repo .env from filling them in
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--no-auto-start", "--delay", "0", "--retry-wait", "0", *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    # ------------------------------------------------------------------ tests

    def test_full_run_maps_every_verdict_and_persists(self):
        ids = {
            "good": self.add(1, "good@a.org"),
            "bad": self.add(2, "bad@b.org"),
            "catchall": self.add(3, "catchall@c.org"),
            "full": self.add(4, "full@d.org"),
            "blocked": self.add(5, "blocked@e.org"),
        }
        out = self.run_cli(expect=0).stdout
        status = {k: self.row(i)["email_status"] for k, i in ids.items()}
        self.assertEqual(status, {"good": "deliverable", "bad": "undeliverable", "catchall": "risky",
                                  "full": "risky", "blocked": "unknown"})
        for key in ("good", "bad", "catchall"):
            row = self.row(ids[key])
            self.assertIsNotNone(row["email_checked_at"], key)
            self.assertEqual(row["email_check_details"]["engine"], "reacher")
        self.assertTrue(self.row(ids["catchall"])["email_check_details"]["needs_review"])
        self.assertFalse(self.row(ids["good"])["email_check_details"]["needs_review"])
        self.assertTrue(self.row(ids["catchall"])["email_check_details"]["catch_all"])
        self.assertIn("3 contact(s) need review", out)  # catchall, full, blocked
        self.assertIn("NEEDS REVIEW", out)

    def test_inferred_candidate_is_promoted_and_recorded(self):
        cid = self.add(1, "bad@a.org", source="inferred", candidates=["bad2@a.org", "good@a.org", "good2@a.org"])
        self.run_cli(expect=0)
        row = self.row(cid)
        self.assertEqual(row["email"], "good@a.org")
        self.assertEqual(row["email_status"], "deliverable")
        checks = {c["email"]: c.get("check") for c in row["candidate_emails"]}
        self.assertEqual(checks, {"bad2@a.org": "invalid", "good@a.org": "safe", "good2@a.org": None})
        self.assertEqual(self.fake.calls, ["bad@a.org", "bad2@a.org", "good@a.org"])

    def test_greylisted_contact_is_retried_and_resolves(self):
        cid = self.add(1, "grey@a.org")
        self.fake.sequence["grey"] = ["grey", "good"]
        out = self.run_cli(expect=0).stdout
        self.assertEqual(self.row(cid)["email_status"], "deliverable")
        self.assertIn("Retry pass 1", out)
        self.assertEqual(self.fake.calls, ["grey@a.org", "grey@a.org"])

    def test_permanent_unknown_is_not_retried(self):
        cid = self.add(1, "blocked@a.org")
        out = self.run_cli(expect=0).stdout
        self.assertEqual(self.row(cid)["email_status"], "unknown")
        self.assertNotIn("Retry pass", out)
        self.assertEqual(len(self.fake.calls), 1)

    def test_retry_rounds_zero_disables_retry(self):
        self.add(1, "grey@a.org")
        out = self.run_cli("--retry-rounds", "0", expect=0).stdout
        self.assertNotIn("Retry pass", out)
        self.assertEqual(len(self.fake.calls), 1)

    def test_default_skips_verified_and_recheck_includes_them(self):
        done = self.add(1, "bad@a.org", status="deliverable")
        pending = self.add(2, "good@b.org")
        unknown = self.add(3, "good@c.org", status="unknown")
        self.run_cli(expect=0)
        self.assertEqual(self.row(done)["email_status"], "deliverable")  # untouched
        self.assertEqual(sorted(self.fake.calls), ["good@b.org", "good@c.org"])
        self.assertEqual(self.row(pending)["email_status"], "deliverable")
        self.assertEqual(self.row(unknown)["email_status"], "deliverable")
        self.fake.calls.clear()
        self.run_cli("--recheck", expect=0)
        self.assertEqual(self.row(done)["email_status"], "undeliverable")  # rechecked: bad@ is invalid
        self.assertIn("bad@a.org", self.fake.calls)

    def test_filters(self):
        primary = self.add(1, "good@a.org", primary=True)
        secondary = self.add(1, "good2@a.org", primary=False)
        other = self.add(2, "good@b.org", primary=False)
        self.run_cli("--primary-only", expect=0)
        self.assertEqual(self.row(secondary)["email_status"], "unverified")
        self.assertEqual(self.row(other)["email_status"], "unverified")
        self.assertEqual(self.row(primary)["email_status"], "deliverable")
        self.run_cli("--business-id", "1", expect=0)
        self.assertEqual(self.row(secondary)["email_status"], "deliverable")
        self.assertEqual(self.row(other)["email_status"], "unverified")
        with self.engine.begin() as conn:
            conn.execute(text("update business_contacts set email_status='unverified'"))
        self.run_cli("--limit", "1", expect=0)
        with self.engine.connect() as conn:
            count = conn.execute(text("select count(*) from business_contacts where email_status='deliverable'")).scalar()
        self.assertEqual(count, 1)

    def test_dry_run_writes_nothing(self):
        cid = self.add(1, "good@a.org")
        out = self.run_cli("--dry-run", expect=0).stdout
        self.assertIn("[deliverable]", out)
        row = self.row(cid)
        self.assertEqual(row["email_status"], "unverified")
        self.assertIsNone(row["email_checked_at"])

    def test_rows_without_email_are_ignored(self):
        with self.engine.begin() as conn:
            conn.execute(text("insert into business_contacts (business_id, name) values (1, 'No Email')"))
        out = self.run_cli(expect=0).stdout
        self.assertIn("No contacts need email verification", out)

    def test_report_csv(self):
        self.add(1, "good@a.org")
        self.add(2, "catchall@b.org")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.csv"
            self.run_cli("--report", str(path), expect=0)
            rows = {r["original_email"]: r for r in csv.DictReader(open(path))}
        self.assertEqual(rows["good@a.org"]["status"], "deliverable")
        self.assertEqual(rows["catchall@b.org"]["status"], "risky")
        self.assertEqual(rows["catchall@b.org"]["catch_all"], "True")
        self.assertTrue(rows["catchall@b.org"]["note"])

    def test_reacher_down_exits_cleanly_without_writing(self):
        cid = self.add(1, "good@a.org")
        result = self.run_cli(reacher_url="http://127.0.0.1:9", expect=1)
        self.assertIn("Cannot reach Reacher", result.stdout)
        self.assertIn("locally in Docker", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(self.row(cid)["email_status"], "unverified")

    def test_reacher_dies_mid_run_stops_and_keeps_earlier_results(self):
        first = self.add(1, "good@a.org")
        second = self.add(2, "dies@b.org")
        third = self.add(3, "good@c.org")
        self.fake.shutdown_on = "dies"  # the server drops this request and is gone, like a crashed container
        result = self.run_cli(expect=1)
        self.assertIn("Stopping", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(self.row(first)["email_status"], "deliverable")  # saved before the failure
        self.assertEqual(self.row(second)["email_status"], "unverified")  # Reacher died: not recorded as "unknown"
        self.assertEqual(self.row(third)["email_status"], "unverified")  # never reached

    def test_port_25_blocked_streak_stops_the_run(self):
        ids = [self.add(i, f"nosmtp@d{i}.org") for i in range(1, 6)]
        result = self.run_cli("--retry-rounds", "0", expect=1)
        self.assertIn("port 25 is probably blocked", result.stdout)
        done = [self.row(i)["email_status"] for i in ids]
        self.assertEqual(done.count("unknown"), 3)  # stopped after the third
        self.assertEqual(done.count("unverified"), 2)

    def test_helo_refusal_warning_is_shown_once(self):
        self.add(1, "blocked@a.org")
        self.add(2, "blocked@b.org")
        out = self.run_cli("--retry-rounds", "0", expect=0).stdout
        self.assertEqual(out.count("refused Reacher's HELO name"), 1)

    def test_timeout_is_unknown_and_retried_once(self):
        cid = self.add(1, "slow@a.org")
        self.fake.delays["slow"] = 3
        out = self.run_cli("--reacher-timeout", "1", "--retry-rounds", "1", expect=0).stdout
        self.assertEqual(self.row(cid)["email_status"], "unknown")
        self.assertIn("timed out", out)
        self.assertEqual(len(self.fake.calls), 2)

    def test_bad_arguments(self):
        for args in (["--max-probes", "0"], ["--reacher-timeout", "0"], ["--delay", "-1"]):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2, args)

    def test_missing_helo_is_refused_when_managing_the_container(self):
        env = {**os.environ, "DATABASE_URL": self.url, "REACHER_URL": self.fake.url, "VERIFY_HELO": "", "VERIFY_MAIL_FROM": ""}
        result = subprocess.run([sys.executable, str(SCRIPT), "--dry-run"], cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("VERIFY_HELO is not set", result.stderr)
        self.assertEqual(self.fake.calls, [])

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
        self.add(1, "good@a.org")
        result = self.run_with_identity("nomx@nowhere.org")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Sender problem", result.stdout)
        self.assertEqual(self.fake.calls, ["nomx@nowhere.org"])  # only the sender was checked, no contact

    def test_working_sender_proceeds_to_contacts(self):
        self.add(1, "good@a.org")
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
        values = v.verify_contact(
            {"email": nowhere, "email_source": "website", "candidate_emails": []},
            client, type("A", (), {"delay": 0, "max_probes": 6})(), {})
        self.assertEqual(values["email_status"], "undeliverable")
        self.assertIn("no mail server", values["email_check_details"]["note"])
        self.assertFalse(values["email_check_details"]["needs_review"])

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
