import csv
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_prospects as bp  # noqa: E402
import verify_contact_emails as v  # noqa: E402

from tests.fakes import FakeReacher  # noqa: E402
from tests.test_verify_e2e import ROOT, docker_ok, free_port  # noqa: E402

SCRIPT = ROOT / "scripts" / "build_prospects.py"


class PureLogicTests(unittest.TestCase):
    def test_candidate_addresses_puts_the_contact_email_first_and_dedupes(self):
        contact = {"email": "Shoaib@x.pk", "candidate_emails": [
            {"email": "shoaib@x.pk"}, {"email": "a@x.pk"}, {"email": "A@x.pk"}, {"email": "b@x.pk"}]}
        self.assertEqual(bp.candidate_addresses(contact), ["Shoaib@x.pk", "a@x.pk", "b@x.pk"])

    def test_found_email_missing_from_candidates_is_still_checked(self):
        contact = {"email": "owner@x.pk", "candidate_emails": [{"email": "other@x.pk"}]}
        self.assertEqual(bp.candidate_addresses(contact)[0], "owner@x.pk")

    def test_junk_entries_are_ignored(self):
        contact = {"email": None, "candidate_emails": [{"email": ""}, {"email": "no-at-sign"}, {}, {"email": None}, {"email": " ok@x.pk "}]}
        self.assertEqual(bp.candidate_addresses(contact), ["ok@x.pk"])
        self.assertEqual(bp.candidate_addresses({"email": None, "candidate_emails": None}), [])

    def test_cap(self):
        contact = {"email": "a@x.pk", "candidate_emails": [{"email": f"c{i}@x.pk"} for i in range(10)]}
        self.assertEqual(len(bp.candidate_addresses(contact, 3)), 3)
        self.assertEqual(len(bp.candidate_addresses(contact, 0)), 11)

    def test_prospect_values_owns_only_verification_columns(self):
        contact = {"id": 7, "business_id": 3, "qualification_score": 78}
        values = bp.prospect_values(contact, "a@x.pk", "deliverable", "NOW")
        self.assertEqual(values, {
            "business_id": 3, "contact_id": 7, "email": "a@x.pk", "email_status": "deliverable",
            "email_verification_provider": "reacher", "email_verified_at": "NOW", "qualification_score": 78,
            "outreach_status": "ready"})
        for later in ("outreach_priority", "outreach_facts", "research_summary", "do_not_contact", "last_contacted_at"):
            self.assertNotIn(later, values)

    def test_outreach_status_for_a_prospect_that_stopped_being_deliverable(self):
        self.assertEqual(bp.OUTREACH_STATUS["deliverable"], "ready")
        self.assertEqual(bp.OUTREACH_STATUS["undeliverable"], "rejected")
        self.assertEqual(bp.OUTREACH_STATUS["risky"], "needs_review")
        self.assertEqual(bp.OUTREACH_STATUS["unknown"], "needs_review")

    def test_record_check_stores_the_verdict_on_the_candidate_only(self):
        when = datetime(2026, 10, 5, tzinfo=timezone.utc)
        contact = {"email": "own@x.pk", "candidate_emails": [{"email": "a@x.pk", "pattern": "p"}, {"email": "b@x.pk"}]}
        values = bp.record_check(contact, "A@x.pk", {"verdict": "invalid", "status": "undeliverable"}, when)
        self.assertEqual(values, {"candidate_emails": [
            {"email": "a@x.pk", "pattern": "p", "check": "invalid", "checked_at": when.isoformat()}, {"email": "b@x.pk"}]})
        self.assertNotIn("check", contact["candidate_emails"][1])

    def test_record_check_updates_the_contacts_own_status_but_never_its_email(self):
        when = datetime(2026, 10, 5, tzinfo=timezone.utc)
        contact = {"email": "own@x.pk", "candidate_emails": []}
        values = bp.record_check(contact, "own@x.pk", {"verdict": "safe", "status": "deliverable"}, when)
        self.assertEqual((values["email_status"], values["email_checked_at"]), ("deliverable", when))
        self.assertNotIn("email", values)

    def test_already_decided(self):
        contact = {"email": "own@x.pk", "email_status": None, "candidate_emails": [
            {"email": "no@x.pk", "check": "invalid"}, {"email": "rk@x.pk", "check": "risky"},
            {"email": "ok@x.pk", "check": "safe"}, {"email": "uk@x.pk", "check": "unknown"}, {"email": "new@x.pk"}]}
        self.assertTrue(bp.already_decided(contact, "no@x.pk", None))
        self.assertTrue(bp.already_decided(contact, "rk@x.pk", None))
        self.assertFalse(bp.already_decided(contact, "uk@x.pk", None))  # unknown is checked again
        self.assertFalse(bp.already_decided(contact, "new@x.pk", None))
        self.assertFalse(bp.already_decided(contact, "ok@x.pk", None))  # a deliverable verdict counts only via prospects
        self.assertTrue(bp.already_decided(contact, "ok@x.pk", "deliverable"))
        self.assertFalse(bp.already_decided(contact, "own@x.pk", None))
        self.assertTrue(bp.already_decided({**contact, "email_status": "undeliverable"}, "own@x.pk", None))
        self.assertFalse(bp.already_decided({**contact, "email_status": "deliverable"}, "own@x.pk", None))

    def test_domain_shortcut(self):
        def outcome(status, summary, retryable=False):
            return {"status": status, "summary": summary, "retryable": retryable}
        self.assertEqual(bp.domain_shortcut(outcome("unknown", None))[0], "unknown")
        self.assertEqual(bp.domain_shortcut(outcome("risky", {"is_catch_all": True}))[0], "risky")
        self.assertEqual(bp.domain_shortcut(outcome("unknown", {"can_connect_smtp": False}))[0], "unknown")
        self.assertEqual(bp.domain_shortcut(outcome("unknown", {"can_connect_smtp": True}, retryable=False))[0], "unknown")
        self.assertIsNone(bp.domain_shortcut(outcome("unknown", {"can_connect_smtp": True}, retryable=True)))  # greylisting
        self.assertIsNone(bp.domain_shortcut(outcome("deliverable", {"can_connect_smtp": True})))
        self.assertIsNone(bp.domain_shortcut(outcome("undeliverable", {"can_connect_smtp": True})))
        self.assertIsNone(bp.domain_shortcut(outcome("risky", {"is_catch_all": False, "has_full_inbox": True})))


class CheckAddressTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeReacher()
        self.addCleanup(self.server.close)
        self.client = v.ReacherClient(self.server.url, 5)

    def test_outcomes(self):
        for local, status, verdict in [("good", "deliverable", "safe"), ("bad", "undeliverable", "invalid"),
                                       ("catchall", "risky", "risky"), ("blocked", "unknown", "unknown")]:
            outcome = v.check_address(f"{local}@x.org", self.client, 0, {})
            self.assertEqual((outcome["status"], outcome["verdict"]), (status, verdict), local)

    def test_retryable_flags(self):
        self.assertTrue(v.check_address("grey@x.org", self.client, 0, {})["retryable"])
        self.assertFalse(v.check_address("blocked@x.org", self.client, 0, {})["retryable"])

    def test_failed_call_is_unknown_without_summary(self):
        self.server.die_on = "drop"
        outcome = v.check_address("drop@x.org", self.client, 0, {})
        self.assertEqual((outcome["status"], outcome["summary"], outcome["retryable"]), ("unknown", None, True))

    def test_dead_server_raises(self):
        self.server.shutdown_on = "drop"
        with self.assertRaises(v.ReacherUnavailable):
            v.check_address("drop@x.org", self.client, 0, {})

    def test_throttles_per_domain(self):
        last = {}
        started = time.monotonic()
        v.check_address("good@x.org", self.client, 0.4, last)
        v.check_address("good2@x.org", self.client, 0.4, last)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)


@unittest.skipUnless(docker_ok(), "Docker is not available")
class ProspectsEndToEndTests(unittest.TestCase):
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
            conn.execute(text("truncate prospects restart identity"))
        self.run_cli("--business-id", "2", "--recheck", expect=0)
        self.assertEqual(set(self.by_email()), {"good@b.org"})
        with self.engine.begin() as conn:
            conn.execute(text("truncate prospects restart identity"))
        self.run_cli("--min-score", "50", "--recheck", expect=0)
        self.assertEqual({r["business_id"] for r in self.rows()}, {1})
        with self.engine.begin() as conn:
            conn.execute(text("truncate prospects restart identity"))
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


if __name__ == "__main__":
    unittest.main()
