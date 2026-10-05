"""Fake Reacher HTTP server used by the verification tests."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def reacher_body(reach, *, role=False, mx=True, error=None, **smtp):
    """A response shaped like reacherhq/backend 0.11."""
    body = {
        "input": "x",
        "is_reachable": reach,
        "misc": {"is_disposable": smtp.pop("disposable", False), "is_role_account": role, "gravatar_url": None},
        "mx": {"accepts_mail": mx, "records": ["mx.fake."]},
        "smtp": {"can_connect_smtp": True, "has_full_inbox": False, "is_catch_all": False,
                 "is_deliverable": reach == "safe", "is_disabled": False, **smtp},
        "syntax": {"is_valid_syntax": True},
        "debug": {"smtp": {"verif_method": {"verif_method": {"hello_name": "mail.test.org"}}}},
    }
    if error:
        body["smtp"] = {"error": {"type": "AsyncSmtpError", "message": error}}
    return body


# local part -> response
SCENARIOS = {
    "good": lambda: reacher_body("safe"),
    "good2": lambda: reacher_body("safe"),
    "bad": lambda: reacher_body("invalid"),
    "bad2": lambda: reacher_body("invalid"),
    "nomx": lambda: reacher_body("invalid", mx=False),
    "disabled": lambda: reacher_body("invalid", is_disabled=True),
    "catchall": lambda: reacher_body("risky", is_catch_all=True),
    "full": lambda: reacher_body("risky", has_full_inbox=True),
    "disposable": lambda: reacher_body("risky", disposable=True),
    "odd": lambda: reacher_body("risky"),
    "info": lambda: reacher_body("safe", role=True),
    "blocked": lambda: reacher_body("unknown", error="permanent: 5.5.2 helo rejected: need fully-qualified hostname"),
    "grey": lambda: reacher_body("unknown", error="transient: 4.7.1 try later"),
    "nosmtp": lambda: {**reacher_body("unknown"), "smtp": {"can_connect_smtp": False, "error": {"message": "connect timed out"}}},
}


class _QuietHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # clients that time out on purpose are not errors
        pass


class FakeReacher:
    """Threaded HTTP server mimicking Reacher. Behaviour toggles are plain attributes."""

    def __init__(self, port=0):
        self.calls: list[str] = []
        self.only_v0 = False
        self.status_override: int | None = None
        self.raw_response: bytes | None = None
        self.delays: dict[str, float] = {}
        self.die_on: str | None = None  # close the socket without answering for this local part
        self.shutdown_on: str | None = None  # same, and take the whole server down afterwards
        self.sequence: dict[str, list] = {}  # local part -> scenario names consumed one per call
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload=None, raw=None):
                data = raw if raw is not None else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/version":
                    self._send(200, {"version": "0.11.6"})
                else:
                    self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                to = json.loads(self.rfile.read(length))["to_email"]
                if self.path not in ("/v1/check_email", "/v0/check_email") or (self.path == "/v1/check_email" and fake.only_v0):
                    return self._send(404, {})
                fake.calls.append(to)
                local = to.split("@")[0]
                if local in (fake.die_on, fake.shutdown_on):
                    if local == fake.shutdown_on:
                        fake.close()  # synchronous: the listener is gone before the client sees the drop
                    self.connection.close()
                    return
                time.sleep(fake.delays.get(local, 0))
                if fake.status_override:
                    return self._send(fake.status_override, {"error": "boom"})
                if fake.raw_response is not None:
                    return self._send(200, raw=fake.raw_response)
                queue = fake.sequence.get(local)
                name = queue.pop(0) if queue else local
                self._send(200, SCENARIOS.get(name, SCENARIOS["bad"])())

        self.server = _QuietHttpServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
