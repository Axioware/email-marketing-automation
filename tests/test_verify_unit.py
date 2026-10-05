import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_contact_emails as v  # noqa: E402

from tests.fakes import FakeReacher, reacher_body  # noqa: E402


def contact(email="good@fake.org", candidates=(), source="website"):
    return {"email": email, "email_source": source, "candidate_emails": [{"email": c} for c in candidates]}


ARGS = SimpleNamespace(delay=0, max_probes=6)


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


class MappingTests(unittest.TestCase):
    def test_reacher_status_mapping(self):
        self.assertEqual(
            v.REACHER_STATUS, {"safe": "deliverable", "invalid": "undeliverable", "risky": "risky", "unknown": "unknown"}
        )
        self.assertEqual(v.NEEDS_REVIEW_STATUSES, {"risky", "unknown"})

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


class VerifyContactReacherTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeReacher()
        self.addCleanup(self.server.close)
        self.client = v.ReacherClient(self.server.url, 5)

    def run_contact(self, *args, **kwargs):
        return v.verify_contact(contact(*args, **kwargs), self.client, ARGS, {})

    def test_each_verdict(self):
        expected = {
            "good": ("deliverable", False), "bad": ("undeliverable", False), "nomx": ("undeliverable", False),
            "catchall": ("risky", True), "full": ("risky", True), "odd": ("risky", True),
            "blocked": ("unknown", True), "grey": ("unknown", True),
        }
        for local, (status, review) in expected.items():
            values = self.run_contact(f"{local}@fake.org")
            self.assertEqual(values["email_status"], status, local)
            self.assertEqual(values["email_check_details"]["needs_review"], review, local)

    def test_safe_never_changes_the_email(self):
        self.assertNotIn("email", self.run_contact("good@fake.org"))

    def test_catch_all_flag_recorded(self):
        details = self.run_contact("catchall@fake.org")["email_check_details"]
        self.assertTrue(details["catch_all"])
        self.assertTrue(details["reacher"]["is_catch_all"])

    def test_role_based_comes_only_from_reacher(self):
        self.assertTrue(self.run_contact("info@fake.org")["email_check_details"]["role_based"])
        self.assertFalse(self.run_contact("good@fake.org")["email_check_details"]["role_based"])
        self.server.raw_response = b'{"is_reachable": "unknown"}'  # Reacher gave no role information
        self.assertFalse(self.run_contact("admin@fake.org")["email_check_details"]["role_based"])  # no local guessing

    def test_retryable_only_for_transient_unknowns(self):
        self.assertTrue(self.run_contact("grey@fake.org")["email_check_details"]["retryable"])
        self.assertFalse(self.run_contact("blocked@fake.org")["email_check_details"]["retryable"])
        self.assertFalse(self.run_contact("good@fake.org")["email_check_details"]["retryable"])

    def test_inferred_candidates_promote_first_safe(self):
        values = self.run_contact("bad@fake.org", ["bad2@fake.org", "good@fake.org", "never@fake.org"], "inferred")
        self.assertEqual(values["email_status"], "deliverable")
        self.assertEqual(values["email"], "good@fake.org")
        self.assertEqual(self.server.calls, ["bad@fake.org", "bad2@fake.org", "good@fake.org"])
        checks = {c["email"]: c.get("check") for c in values["candidate_emails"]}
        self.assertEqual(checks, {"bad2@fake.org": "invalid", "good@fake.org": "safe", "never@fake.org": None})

    def test_catch_all_stops_the_candidate_search(self):
        values = self.run_contact("catchall@fake.org", ["good@fake.org"], "inferred")
        self.assertEqual(values["email_status"], "risky")
        self.assertNotIn("email", values)
        self.assertEqual(self.server.calls, ["catchall@fake.org"])

    def test_blocked_and_grey_stop_the_candidate_search(self):
        for local in ("blocked", "grey", "nosmtp"):
            self.server.calls.clear()
            values = self.run_contact(f"{local}@fake.org", ["good@fake.org"], "inferred")
            self.assertEqual(values["email_status"], "unknown", local)
            self.assertEqual(len(self.server.calls), 1, local)

    def test_website_email_ignores_candidates(self):
        values = self.run_contact("bad@fake.org", ["good@fake.org"], "website")
        self.assertEqual(values["email_status"], "undeliverable")
        self.assertEqual(self.server.calls, ["bad@fake.org"])

    def test_all_invalid_is_undeliverable_with_count(self):
        values = self.run_contact("bad@fake.org", ["bad2@fake.org"], "inferred")
        self.assertEqual(values["email_status"], "undeliverable")
        self.assertIn("2 candidate", values["email_check_details"]["note"])

    def test_max_probes_caps_calls(self):
        args = SimpleNamespace(delay=0, max_probes=2)
        v.verify_contact(contact("bad@fake.org", ["bad2@fake.org", "good@fake.org"], "inferred"),
                                 self.client, args, {})
        self.assertEqual(len(self.server.calls), 2)

    def test_syntax_is_left_to_reacher(self):
        values = self.run_contact("not@@valid")
        self.assertEqual(self.server.calls, ["not@@valid"])  # no local pre-check
        self.assertEqual(values["email_status"], "undeliverable")

    def test_timeout_marks_unknown_retryable(self):
        self.server.delays["slow"] = 3
        client = v.ReacherClient(self.server.url, 1)
        values = v.verify_contact(contact("slow@fake.org"), client, ARGS, {})
        self.assertEqual(values["email_status"], "unknown")
        self.assertTrue(values["email_check_details"]["retryable"])
        self.assertTrue(v.was_retryable(values))

    def test_per_domain_throttle(self):
        args = SimpleNamespace(delay=0.4, max_probes=6)
        last = {}
        started = time.monotonic()
        v.verify_contact(contact("good@fake.org"), self.client, args, last)
        v.verify_contact(contact("good2@fake.org"), self.client, args, last)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        started = time.monotonic()
        v.verify_contact(contact("good@other.org"), self.client, args, last)
        self.assertLess(time.monotonic() - started, 0.4)  # a different domain is not throttled

    def test_dropped_connection_marks_unknown_and_retryable(self):
        self.server.die_on = "drop"
        values = self.run_contact("drop@fake.org")
        self.assertEqual(values["email_status"], "unknown")
        self.assertTrue(values["email_check_details"]["retryable"])

    def test_check_failure_with_dead_server_raises_unavailable(self):
        self.server.shutdown_on = "drop"
        with self.assertRaises(v.ReacherUnavailable):
            self.run_contact("drop@fake.org")

    def test_unavailable_propagates(self):
        client = v.ReacherClient("http://127.0.0.1:9", 2)
        with self.assertRaises(v.ReacherUnavailable):
            v.verify_contact(contact("good@fake.org"), client, ARGS, {})

    def test_retry_predicate(self):
        retryable = {"email_status": "unknown", "email_check_details": {"retryable": True}}
        self.assertTrue(v.was_retryable(retryable))
        self.assertFalse(v.was_retryable({"email_status": "unknown", "email_check_details": {"retryable": False}}))
        self.assertFalse(v.was_retryable({"email_status": "deliverable", "email_check_details": {"retryable": True}}))


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
