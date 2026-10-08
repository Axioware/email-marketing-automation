import json
import time
from datetime import datetime, timezone
import unittest
from types import SimpleNamespace
from unittest import mock

from pipeline.services import verification as v
from tests.fakes import FakeReacher, reacher_body


def contact(email="good@fake.org", candidates=(), source="website"):
    return {"email": email, "email_source": source, "candidate_emails": [{"email": c} for c in candidates]}




class SenderGuardTests(unittest.TestCase):
    def test_reserved_domains(self):
        for host in ("example.org", "EXAMPLE.com", "mail.example.net", "foo.invalid", "x.test", "host.local", "localhost", "a.b.example"):
            self.assertTrue(v.is_reserved_domain(host), host)
        for host in ("mydomain.dev", "smilesolutions.pk", "example.co.uk", "testing.com", "mylocal.com"):
            self.assertFalse(v.is_reserved_domain(host), host)

    def setUp(self):
        self.server = FakeReacher()
        self.addCleanup(self.server.close)
        self.client = v.ReacherClient(self.server.url, 5)

    def test_sender_that_accepts_mail_is_fine(self):
        self.assertIsNone(v.check_sender(self.client, "good@mine.org"))

    def test_sender_domain_without_mail_is_reported(self):
        self.assertIn("does not accept mail", v.check_sender(self.client, "nomx@nowhere.org"))

    def test_sender_with_bad_syntax_is_reported(self):
        self.server.raw_response = json.dumps({"is_reachable": "invalid", "syntax": {"is_valid_syntax": False}}).encode()
        self.assertIn("not a valid email", v.check_sender(self.client, "oops"))

    def test_slow_or_broken_check_is_not_a_problem(self):
        self.server.status_override = 500
        self.assertIsNone(v.check_sender(self.client, "good@mine.org"))


class PureLogicTests(unittest.TestCase):
    def test_candidate_addresses_puts_the_contact_email_first_and_dedupes(self):
        contact = {"email": "Shoaib@x.pk", "candidate_emails": [
            {"email": "shoaib@x.pk"}, {"email": "a@x.pk"}, {"email": "A@x.pk"}, {"email": "b@x.pk"}]}
        self.assertEqual(v.candidate_addresses(contact), ["Shoaib@x.pk", "a@x.pk", "b@x.pk"])

    def test_found_email_missing_from_candidates_is_still_checked(self):
        contact = {"email": "owner@x.pk", "candidate_emails": [{"email": "other@x.pk"}]}
        self.assertEqual(v.candidate_addresses(contact)[0], "owner@x.pk")

    def test_junk_entries_are_ignored(self):
        contact = {"email": None, "candidate_emails": [{"email": ""}, {"email": "no-at-sign"}, {}, {"email": None}, {"email": " ok@x.pk "}]}
        self.assertEqual(v.candidate_addresses(contact), ["ok@x.pk"])
        self.assertEqual(v.candidate_addresses({"email": None, "candidate_emails": None}), [])

    def test_cap(self):
        contact = {"email": "a@x.pk", "candidate_emails": [{"email": f"c{i}@x.pk"} for i in range(10)]}
        self.assertEqual(len(v.candidate_addresses(contact, 3)), 3)
        self.assertEqual(len(v.candidate_addresses(contact, 0)), 11)

    def test_prospect_values_owns_only_verification_columns(self):
        contact = {"id": 7, "business_id": 3, "qualification_score": 78}
        outcome = {"status": "risky", "reason": "catch_all", "note": "catch-all", "summary": {"is_catch_all": True}}
        values = v.prospect_values(contact, "a@x.pk", outcome, "NOW")
        self.assertEqual(values, {
            "business_id": 3, "contact_id": 7, "email": "a@x.pk", "email_status": "risky", "verdict": "catch_all",
            "verification_note": "catch-all", "verification_details": {"is_catch_all": True, "probed": True},
            "email_verification_provider": "reacher", "email_verified_at": "NOW", "qualification_score": 78,
            "outreach_status": "needs_review"})
        shortcut = {"status": "unknown", "reason": "smtp_unreachable", "note": "not checked", "summary": None}
        values = v.prospect_values(contact, "b@x.pk", shortcut, "NOW", probed=False)
        self.assertEqual((values["verification_details"], values["outreach_status"]), ({"probed": False}, "needs_review"))
        for later in ("outreach_priority", "outreach_facts", "research_summary", "do_not_contact", "last_contacted_at"):
            self.assertNotIn(later, values)

    def test_outreach_status_for_a_prospect_that_stopped_being_deliverable(self):
        self.assertEqual(v.OUTREACH_STATUS["deliverable"], "ready")
        self.assertEqual(v.OUTREACH_STATUS["undeliverable"], "rejected")
        self.assertEqual(v.OUTREACH_STATUS["risky"], "needs_review")
        self.assertEqual(v.OUTREACH_STATUS["unknown"], "needs_review")

    def test_record_check_stores_the_verdict_on_the_candidate_only(self):
        when = datetime(2026, 10, 5, tzinfo=timezone.utc)
        contact = {"email": "own@x.pk", "candidate_emails": [{"email": "a@x.pk", "pattern": "p"}, {"email": "b@x.pk"}]}
        values = v.record_check(contact, "A@x.pk", {"verdict": "invalid", "status": "undeliverable"}, when)
        self.assertEqual(values, {"candidate_emails": [
            {"email": "a@x.pk", "pattern": "p", "check": "invalid", "checked_at": when.isoformat()}, {"email": "b@x.pk"}]})
        self.assertNotIn("check", contact["candidate_emails"][1])

    def test_record_check_updates_the_contacts_own_status_but_never_its_email(self):
        when = datetime(2026, 10, 5, tzinfo=timezone.utc)
        contact = {"email": "own@x.pk", "candidate_emails": []}
        values = v.record_check(contact, "own@x.pk", {"verdict": "safe", "status": "deliverable"}, when)
        self.assertEqual((values["email_status"], values["email_checked_at"]), ("deliverable", when))
        self.assertNotIn("email", values)

    def test_already_decided(self):
        for status in ("deliverable", "undeliverable", "risky"):
            self.assertTrue(v.already_decided(status), status)
        for status in ("unknown", None):  # unknown, and addresses never stored, are checked (again)
            self.assertFalse(v.already_decided(status), status)

    def test_verdicts(self):
        from tests.fakes import SCENARIOS
        expected = {"good": "deliverable", "bad": "mailbox_not_found", "nomx": "no_mail_server", "disabled": "disabled",
                    "catchall": "catch_all", "full": "full_inbox", "disposable": "disposable", "odd": "risky",
                    "info": "deliverable", "blocked": "blocked", "grey": "temporary_failure",
                    "nosmtp": "smtp_unreachable"}
        for name, code in expected.items():
            self.assertEqual(v.reacher_verdict(v.summarize_reacher(SCENARIOS[name]())), code, name)
        self.assertEqual(v.reacher_verdict(None), "check_failed")
        invalid_syntax = {**v.summarize_reacher(SCENARIOS["bad"]()), "valid_syntax": False}
        self.assertEqual(v.reacher_verdict(invalid_syntax), "invalid_syntax")
        from pipeline.models import Prospect
        codes = {*expected.values(), "check_failed", "invalid_syntax", "unknown"}
        self.assertLessEqual(codes, set(Prospect.Verdict.values))  # every code has a label in the admin

    def test_domain_shortcut(self):
        def outcome(status, summary, retryable=False):
            return {"status": status, "summary": summary, "retryable": retryable}
        self.assertEqual(v.domain_shortcut(outcome("unknown", None))[0], "unknown")
        self.assertEqual(v.domain_shortcut(outcome("risky", {"is_catch_all": True}))[0], "risky")
        self.assertEqual(v.domain_shortcut(outcome("risky", {"is_catch_all": True}))[2], "catch_all")
        self.assertEqual(v.domain_shortcut(outcome("unknown", {"can_connect_smtp": False}))[2], "smtp_unreachable")
        self.assertEqual(v.domain_shortcut(outcome("unknown", {"can_connect_smtp": False}))[0], "unknown")
        self.assertEqual(v.domain_shortcut(outcome("unknown", {"can_connect_smtp": True}, retryable=False))[0], "unknown")
        self.assertIsNone(v.domain_shortcut(outcome("unknown", {"can_connect_smtp": True}, retryable=True)))  # greylisting
        self.assertIsNone(v.domain_shortcut(outcome("deliverable", {"can_connect_smtp": True})))
        self.assertIsNone(v.domain_shortcut(outcome("undeliverable", {"can_connect_smtp": True})))
        self.assertIsNone(v.domain_shortcut(outcome("risky", {"is_catch_all": False, "has_full_inbox": True})))


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


class MappingTests(unittest.TestCase):
    def test_reacher_status_mapping(self):
        self.assertEqual(
            v.REACHER_STATUS, {"safe": "deliverable", "invalid": "undeliverable", "risky": "risky", "unknown": "unknown"}
        )

    def test_notes_cover_every_verdict(self):
        cases = [
            (reacher_body("safe"), None),
            (reacher_body("invalid"), "does not exist"),
            (reacher_body("invalid", mx=False), "no mail server"),
            (reacher_body("invalid", is_disabled=True), "disabled"),
            (reacher_body("risky", is_catch_all=True), "catch-all"),
            (reacher_body("risky", has_full_inbox=True), "full"),
            (reacher_body("risky", disposable=True), "disposable"),
            (reacher_body("risky"), "risky"),
            (reacher_body("unknown", error="permanent: 5.5.2 helo"), "helo"),
            (reacher_body("unknown"), "could not verify"),
        ]
        for body, expected in cases:
            note = v.reacher_note(v.summarize_reacher(body))
            if expected is None:
                self.assertIsNone(note)
            else:
                self.assertIn(expected, note.lower(), body["is_reachable"])

    def test_summary_handles_error_only_and_missing_sections(self):
        summary = v.summarize_reacher({"is_reachable": "unknown", "smtp": {"error": {"message": "boom"}}})
        self.assertEqual(summary["smtp_error"], "boom")
        self.assertIsNone(summary["can_connect_smtp"])
        empty = v.summarize_reacher({})
        self.assertIsNone(empty["is_reachable"])
        self.assertIn("could not verify", v.reacher_note(empty))

    def test_summary_drops_noise(self):
        summary = v.summarize_reacher({**reacher_body("safe"), "misc": {"gravatar_url": "x", "haveibeenpwned": True}})
        self.assertNotIn("gravatar_url", summary)
        self.assertNotIn("debug", summary)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeReacher()
        self.addCleanup(self.server.close)

    def client(self, **kwargs):
        return v.ReacherClient(self.server.url, kwargs.pop("timeout", 5), **kwargs)

    def test_version(self):
        self.assertEqual(self.client().version(), "0.11.6")

    def test_version_down(self):
        with self.assertRaises(v.ReacherUnavailable):
            v.ReacherClient("http://127.0.0.1:9", 2).version()

    def test_check_remembers_endpoint(self):
        client = self.client()
        self.assertEqual(client.check("good@x.org")["is_reachable"], "safe")
        self.assertEqual(client.path, "/v1/check_email")

    def test_falls_back_to_v0(self):
        self.server.only_v0 = True
        client = self.client()
        self.assertEqual(client.check("good@x.org")["is_reachable"], "safe")
        self.assertEqual(client.path, "/v0/check_email")

    def test_bare_check_email_path_is_not_used(self):
        self.assertNotIn("/check_email", v.REACHER_CHECK_PATHS)

    def test_timeout_is_per_check_error(self):
        self.server.delays["slow"] = 3
        with self.assertRaises(v.ReacherError):
            self.client(timeout=1).check("slow@x.org")

    def test_http_500_is_per_check_error(self):
        self.server.status_override = 500
        with self.assertRaises(v.ReacherError):
            self.client().check("good@x.org")

    def test_dropped_connection_is_per_check_error_not_a_crash(self):
        self.server.die_on = "drop"
        with self.assertRaises(v.ReacherError) as caught:
            self.client().check("drop@x.org")
        self.assertIn("dropped the connection", str(caught.exception))
        self.assertEqual(self.client().check("good@x.org")["is_reachable"], "safe")  # server still fine

    def test_garbage_json_is_per_check_error(self):
        self.server.raw_response = b"<html>nope</html>"
        with self.assertRaises(v.ReacherError):
            self.client().check("good@x.org")

    def test_server_down_during_check_is_unavailable(self):
        with self.assertRaises(v.ReacherUnavailable):
            v.ReacherClient("http://127.0.0.1:9", 2).check("a@b.org")


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class EnsureReacherTests(unittest.TestCase):
    """Docker is mocked: these check which docker commands run, not Docker itself."""

    WANT = {"RCH__FROM_EMAIL": "verify@me.org", "RCH__HELLO_NAME": "mail.me.org"}

    def setUp(self):
        self.commands = []
        self.versions = []  # successive client.version() outcomes: str or exception
        self.inspect = None  # None -> container missing, else dict with env/running

        def docker(*args, timeout=600):
            self.commands.append(args)
            if args[0] == "inspect":
                if self.inspect is None:
                    return completed(1, stderr="No such object")
                env = [f"{k}={val}" for k, val in self.inspect["env"].items()]
                payload = [{"Config": {"Env": env}, "State": {"Running": self.inspect["running"]}}]
                import json
                return completed(0, json.dumps(payload))
            return completed(0)

        patches = [
            mock.patch.object(v, "_docker", docker),
            mock.patch.object(v.shutil, "which", lambda name: "/usr/bin/docker"),
            mock.patch.object(v.time, "sleep", lambda s: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        client = mock.Mock(spec=v.ReacherClient)
        client.base_url = "http://127.0.0.1:8080"

        def version():
            outcome = self.versions.pop(0) if self.versions else v.ReacherUnavailable("down")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client.version.side_effect = version
        self.client = client

    def ensure(self, **kwargs):
        defaults = dict(mail_from="verify@me.org", helo="mail.me.org", auto_start=True, wait=5)
        defaults.update(kwargs)
        return v.ensure_reacher(self.client, defaults.pop("mail_from"), defaults.pop("helo"), defaults.pop("auto_start"),
                                **defaults)

    def verbs(self):
        return [c[0] for c in self.commands if c[0] != "inspect"]

    def test_running_and_correct_does_nothing(self):
        self.versions = ["0.11.6"]
        self.inspect = {"env": self.WANT, "running": True}
        self.assertEqual(self.ensure(), "0.11.6")
        self.assertEqual(self.verbs(), [])

    def test_wrong_identity_recreates_the_container(self):
        self.versions = ["0.11.6"]
        self.inspect = {"env": {"RCH__FROM_EMAIL": "verify@example.org", "RCH__HELLO_NAME": "mail.me.org"}, "running": True}
        with mock.patch("builtins.print") as out:
            self.assertEqual(self.ensure(), "0.11.6")
        self.assertEqual(self.verbs(), ["rm", "run"])
        run = next(c for c in self.commands if c[0] == "run")
        self.assertIn("RCH__FROM_EMAIL=verify@me.org", run)
        self.assertIn("RCH__HELLO_NAME=mail.me.org", run)
        self.assertIn("Recreating", " ".join(str(c) for c in out.call_args_list))

    def test_running_container_not_yet_answering_is_waited_for(self):
        self.versions = [v.ReacherUnavailable("booting"), "0.11.6"]
        self.inspect = {"env": self.WANT, "running": True}
        self.assertEqual(self.ensure(), "0.11.6")
        self.assertEqual(self.verbs(), [])

    def test_missing_container_is_created_with_identity_and_loopback_port(self):
        self.versions = [v.ReacherUnavailable("down"), "0.11.6"]
        self.ensure()
        run = next(c for c in self.commands if c[0] == "run")
        self.assertEqual(self.verbs(), ["run"])
        self.assertIn("127.0.0.1:8080:8080", run)
        self.assertIn("unless-stopped", run)
        self.assertEqual(run[-1], v.REACHER_IMAGE)
        self.assertIn("RCH__FROM_EMAIL=verify@me.org", run)

    def test_stopped_matching_container_is_started(self):
        self.versions = [v.ReacherUnavailable("down"), "0.11.6"]
        self.inspect = {"env": self.WANT, "running": False}
        self.ensure()
        self.assertEqual(self.verbs(), ["start"])

    def test_stopped_mismatched_container_is_replaced(self):
        self.versions = [v.ReacherUnavailable("down"), "0.11.6"]
        self.inspect = {"env": {"RCH__FROM_EMAIL": "old@x.org"}, "running": False}
        self.ensure()
        self.assertEqual(self.verbs(), ["rm", "run"])

    def test_custom_port_is_published(self):
        self.client.base_url = "http://localhost:9099"
        self.versions = [v.ReacherUnavailable("down"), "0.11.6"]
        self.ensure()
        self.assertIn("127.0.0.1:9099:8080", next(c for c in self.commands if c[0] == "run"))

    def test_no_identity_warns_about_localhost(self):
        self.versions = [v.ReacherUnavailable("down"), "0.11.6"]
        with mock.patch("builtins.print") as out:
            self.ensure(mail_from=None, helo=None)
        self.assertIn("localhost", " ".join(str(c) for c in out.call_args_list))
        self.assertFalse(any(a.startswith("RCH__") for a in next(c for c in self.commands if c[0] == "run")))

    def test_no_auto_start_only_checks_health(self):
        self.versions = ["0.11.6"]
        self.assertEqual(self.ensure(auto_start=False), "0.11.6")
        self.assertEqual(self.commands, [])

    def test_no_auto_start_fails_when_down(self):
        with self.assertRaises(v.ReacherUnavailable):
            self.ensure(auto_start=False)
        self.assertEqual(self.commands, [])

    def test_remote_url_is_rejected(self):
        for url in ("https://reacher.example.com", "https://api.reacher.email"):
            self.client.base_url = url
            with self.assertRaises(v.ReacherUnavailable) as caught:
                self.ensure()
            self.assertIn("is not local", str(caught.exception))
        self.assertEqual(self.commands, [])

    def test_no_docker(self):
        with mock.patch.object(v.shutil, "which", lambda name: None):
            with self.assertRaises(v.ReacherUnavailable) as caught:
                self.ensure()
        self.assertIn("Docker is not installed", str(caught.exception))

    def test_docker_run_failure_is_reported(self):
        original = v._docker

        def failing(*args, timeout=600):
            return completed(125, stderr="x\nport is already allocated") if args[0] == "run" else original(*args, timeout=timeout)

        with mock.patch.object(v, "_docker", failing):
            with self.assertRaises(v.ReacherUnavailable) as caught:
                self.ensure()
        self.assertIn("port is already allocated", str(caught.exception))

    def test_started_but_never_answers(self):
        with mock.patch.object(v.time, "monotonic", side_effect=[0, 1, 2, 3, 100, 200]):
            with self.assertRaises(v.ReacherUnavailable) as caught:
                self.ensure(wait=10)
        self.assertIn("did not answer", str(caught.exception))

    def test_config_mismatch_helper(self):
        self.assertEqual(v.config_mismatch(self.WANT, self.WANT), [])
        self.assertEqual(len(v.config_mismatch({}, self.WANT)), 2)
        self.assertEqual(v.config_mismatch({"A": "1"}, {}), [])
        self.assertEqual(v.reacher_env(None, None), {})
        self.assertEqual(v.reacher_env("a@b.org", "h.b.org"), self.WANT | {"RCH__FROM_EMAIL": "a@b.org", "RCH__HELLO_NAME": "h.b.org"})


if __name__ == "__main__":
    unittest.main()
