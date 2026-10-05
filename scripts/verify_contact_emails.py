"""Verify contact emails with a self-hosted Reacher server (reacherhq/backend in Docker).

All verification is done by Reacher. This script only sets Reacher up, asks it about each address, maps
its verdict to `email_status` and stores the result. Nothing here probes mail servers itself and no email
is ever sent.
"""
import argparse
import csv
import http.client
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, create_engine, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

REACHER_IMAGE = "reacherhq/backend:latest"
REACHER_CONTAINER = "reacher"
REACHER_DEFAULT_URL = "http://127.0.0.1:8080"
REACHER_CHECK_PATHS = ("/v1/check_email", "/v0/check_email")  # current image first; the bare /check_email is a 404
REACHER_START_WAIT = 90.0
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Consumer mailbox providers: servers check SPF for the sender and our IP is not authorised to send as these.
CONSUMER_SENDER_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "aol.com", "icloud.com"}

# RFC 2606/6761 reserved names: they cannot receive mail, so servers reject probes sent from them.
RESERVED_DOMAINS = {"example.com", "example.net", "example.org", "localhost"}
RESERVED_TLDS = {"example", "invalid", "test", "localhost", "local"}

DEFAULT_MAX_PROBES = 6
DEFAULT_DELAY = 2.0
DEFAULT_RETRY_ROUNDS = 1
DEFAULT_RETRY_WAIT = 120.0
DEFAULT_REACHER_TIMEOUT = 90.0  # a real SMTP conversation with a slow server can take a minute
MAX_CONSECUTIVE_NO_SMTP = 3
VERIFIABLE_STATUSES = ("unverified", "unknown")

REACHER_STATUS = {"safe": "deliverable", "invalid": "undeliverable", "risky": "risky", "unknown": "unknown"}
NEEDS_REVIEW_STATUSES = {"risky", "unknown"}  # held for review, never used automatically


class ReacherUnavailable(RuntimeError):
    """The Reacher server cannot be used (not running, cannot be started). Stops the whole run."""


class ReacherError(RuntimeError):
    """One check failed (timeout, server error). The contact is marked unknown and can be retried."""


# ---------------------------------------------------------------- Reacher HTTP client


class ReacherClient:
    """Minimal client for the local reacherhq/backend HTTP API (https://github.com/reacherhq/check-if-email-exists)."""

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.path: str | None = None  # remembered after the first successful call

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urlopen(request, timeout=timeout or self.timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def version(self) -> str:
        """Health check. Raises ReacherUnavailable when the server cannot be reached."""
        try:
            return str(self._request("GET", "/version", timeout=5).get("version", "unknown"))
        except HTTPError as error:
            raise ReacherUnavailable(f"Reacher at {self.base_url} answered HTTP {error.code} to /version.") from error
        except (URLError, OSError, ValueError, http.client.HTTPException) as error:
            raise ReacherUnavailable(f"Cannot reach Reacher at {self.base_url} ({type(error).__name__}).") from error

    def check(self, address: str) -> dict:
        paths = (self.path,) if self.path else REACHER_CHECK_PATHS
        for path in paths:
            try:
                result = self._request("POST", path, {"to_email": address})
                self.path = path
                return result
            except HTTPError as error:
                if error.code == 404 and path != paths[-1]:
                    continue  # older/newer image exposing the other version prefix
                raise ReacherError(f"Reacher returned HTTP {error.code}") from error
            except (socket.timeout, TimeoutError) as error:
                raise ReacherError("Reacher request timed out") from error
            except (ConnectionError, http.client.HTTPException) as error:
                # urllib only wraps errors raised while sending; a connection dropped while Reacher is
                # answering escapes raw. One dropped answer is a per-address failure, not a dead server.
                raise ReacherError(f"Reacher dropped the connection ({type(error).__name__})") from error
            except URLError as error:
                if isinstance(error.reason, (socket.timeout, TimeoutError)):
                    raise ReacherError("Reacher request timed out") from error
                raise ReacherUnavailable(f"Lost connection to Reacher at {self.base_url} ({type(error.reason).__name__}).") from error
            except ValueError as error:
                raise ReacherError("Reacher returned an unreadable response") from error
        raise ReacherError("Reacher exposes no check_email endpoint")


# ---------------------------------------------------------------- Docker setup


def reacher_env(mail_from: str | None, helo: str | None) -> dict[str, str]:
    """Container settings that give Reacher a real sender identity (it ignores them per request)."""
    env = {}
    if mail_from:
        env["RCH__FROM_EMAIL"] = mail_from
    if helo:
        env["RCH__HELLO_NAME"] = helo
    return env


def _docker(*args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _last_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else "docker error"


def container_config(name: str) -> dict | None:
    """None when the container does not exist, else {'running': bool, 'env': {...}}."""
    try:
        result = _docker("inspect", name, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)[0]
    except (ValueError, IndexError):
        return None
    env = dict(item.split("=", 1) for item in info.get("Config", {}).get("Env") or [] if "=" in item)
    return {"running": bool(info.get("State", {}).get("Running")), "env": env}


def config_mismatch(current: dict[str, str], wanted: dict[str, str]) -> list[str]:
    return [f"{key} is {current.get(key)!r}, expected {value!r}" for key, value in wanted.items() if current.get(key) != value]


def ensure_reacher(client: ReacherClient, mail_from: str | None, helo: str | None, auto_start: bool,
                   container: str = REACHER_CONTAINER, wait: float = REACHER_START_WAIT) -> str:
    """Make sure a Reacher container configured with the sender identity answers at client.base_url.

    Creates the container if missing, starts it if stopped, and recreates it when its sender/HELO settings
    differ from the wanted ones (a wrong HELO makes mail servers refuse every check, giving false results).
    Returns the Reacher version. Raises ReacherUnavailable with an actionable message otherwise.
    """
    wanted = reacher_env(mail_from, helo)
    parts = urlsplit(client.base_url)
    if (parts.hostname or "") not in LOCAL_HOSTS:
        raise ReacherUnavailable(f"REACHER_URL {client.base_url} is not local; only a local Docker Reacher is supported.")
    if not auto_start:
        return client.version()  # raises ReacherUnavailable when it is not running
    if shutil.which("docker") is None:
        raise ReacherUnavailable("Docker is not installed, so Reacher cannot be started here.")

    config = container_config(container)
    try:
        if config:
            problems = config_mismatch(config["env"], wanted)
            if problems:
                print(f"Recreating the '{container}' container: {'; '.join(problems)}.")
                _docker("rm", "-f", container, timeout=60)
                config = None
        if config and config["running"]:
            try:
                return client.version()
            except ReacherUnavailable:
                pass  # running but not answering yet; fall through and wait below
        elif config:
            print(f"Starting existing Reacher container '{container}'...")
            result = _docker("start", container, timeout=60)
            if result.returncode != 0:
                raise ReacherUnavailable(f"Could not start Reacher: {_last_line(result.stderr)}")
        else:
            if not wanted:
                print("Warning: no sender identity configured (VERIFY_MAIL_FROM / VERIFY_HELO); Reacher will announce "
                      "'localhost', which many mail servers refuse.")
            print(f"Starting Reacher container '{container}' ({REACHER_IMAGE})...")
            command = ["run", "-d", "--name", container, "--restart", "unless-stopped",
                       "-p", f"127.0.0.1:{parts.port or 8080}:8080"]
            for key, value in wanted.items():
                command += ["-e", f"{key}={value}"]
            result = _docker(*command, REACHER_IMAGE)
            if result.returncode != 0:
                raise ReacherUnavailable(f"Could not start Reacher: {_last_line(result.stderr)}")
    except (OSError, subprocess.SubprocessError) as error:
        raise ReacherUnavailable(f"Docker failed ({type(error).__name__}): {error}") from error

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            return client.version()
        except ReacherUnavailable:
            time.sleep(2)
    raise ReacherUnavailable(f"Reacher container '{container}' started but did not answer within {wait:.0f}s (see `docker logs {container}`).")


# ---------------------------------------------------------------- verdict handling


def summarize_reacher(result: dict) -> dict:
    """The fields worth keeping from a Reacher response (drops gravatar, breach data and debug internals)."""
    smtp, mx = result.get("smtp") or {}, result.get("mx") or {}
    misc, syntax = result.get("misc") or {}, result.get("syntax") or {}
    return {
        "is_reachable": result.get("is_reachable"),
        "can_connect_smtp": smtp.get("can_connect_smtp"),
        "is_deliverable": smtp.get("is_deliverable"),
        "is_catch_all": smtp.get("is_catch_all"),
        "has_full_inbox": smtp.get("has_full_inbox"),
        "is_disabled": smtp.get("is_disabled"),
        "smtp_error": (smtp.get("error") or {}).get("message") or (mx.get("error") or {}).get("message"),
        "accepts_mail": mx.get("accepts_mail"),
        "is_disposable": misc.get("is_disposable"),
        "is_role_account": misc.get("is_role_account"),
        "valid_syntax": syntax.get("is_valid_syntax"),
    }


def reacher_note(summary: dict) -> str | None:
    """A human-readable reason for any verdict other than safe."""
    reachable = summary["is_reachable"]
    if reachable == "safe":
        return None
    if reachable == "invalid":
        if summary["valid_syntax"] is False:
            return "invalid syntax"
        if summary["accepts_mail"] is False:
            return "domain has no mail server"
        if summary["is_disabled"]:
            return "mailbox is disabled"
        return "mail server says this mailbox does not exist"
    if reachable == "risky":
        if summary["is_catch_all"]:
            return "domain accepts mail for any address (catch-all); existence cannot be proven"
        if summary["has_full_inbox"]:
            return "mailbox exists but is full"
        if summary["is_disposable"]:
            return "disposable email domain"
        return "Reacher rated this address risky (deliverability not certain)"
    return summary["smtp_error"] or "Reacher could not verify this address (server unreachable, blocked or greylisted)"


def is_reserved_domain(host: str) -> bool:
    host = host.casefold().rstrip(".")
    labels = host.split(".")
    parents = {".".join(labels[i:]) for i in range(len(labels))}  # the name itself and every parent domain
    return bool(parents & RESERVED_DOMAINS) or labels[-1] in RESERVED_TLDS


def check_sender(client: ReacherClient, mail_from: str) -> str | None:
    """Ask Reacher whether the sender's own domain accepts mail. Returns a problem description, or None.

    Mail servers reject probes whose sender domain cannot receive mail, and Reacher reports that rejection
    as an invalid mailbox, which would turn every real address into a false "undeliverable". A failed
    or slow check is not treated as a problem (the sender's own server may simply be slow).
    """
    try:
        summary = summarize_reacher(client.check(mail_from))
    except ReacherError:
        return None
    if summary["valid_syntax"] is False:
        return f"'{mail_from}' is not a valid email address"
    if summary["accepts_mail"] is False:
        return f"the domain of '{mail_from}' does not accept mail"
    return None


def candidate_list(contact: dict, max_probes: int) -> list[str]:
    """Current email first, then the other guesses (inferred emails only), de-duplicated."""
    addresses = [contact["email"]]
    if contact["email_source"] in ("inferred", "pattern"):
        addresses += [item["email"] for item in contact["candidate_emails"] or [] if item.get("email")]
    seen, ordered = set(), []
    for address in addresses:
        key = address.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(address)
    return ordered[:max_probes]


def check_address(address: str, client: ReacherClient, delay: float, last_call: dict[str, float]) -> dict:
    """Ask Reacher about one address (spaced out per domain). Returns the mapped outcome.

    {"address", "status" (deliverable/undeliverable/risky/unknown), "verdict" (Reacher's is_reachable or
    "unknown"), "summary" (None when the call itself failed), "note", "retryable"}. A failed call is reported as
    unknown, unless Reacher itself died, in which case ReacherUnavailable propagates.
    """
    domain = address.rpartition("@")[2].casefold()
    wait = delay - (time.monotonic() - last_call.get(domain, 0.0))
    if wait > 0:
        time.sleep(wait)
    try:
        result = client.check(address)
    except ReacherError as error:
        client.version()  # raises ReacherUnavailable if Reacher itself died, so the address is not recorded as unknown
        return {"address": address, "status": "unknown", "verdict": "unknown", "summary": None,
                "note": str(error), "retryable": True}
    finally:
        last_call[domain] = time.monotonic()
    summary = summarize_reacher(result)
    status = REACHER_STATUS.get(summary["is_reachable"], "unknown")
    retryable = status == "unknown" and not (summary["smtp_error"] or "").startswith("permanent")
    return {"address": address, "status": status, "verdict": summary["is_reachable"] or "unknown",
            "summary": summary, "note": reacher_note(summary), "retryable": retryable}


def verify_contact(contact: dict, client: ReacherClient, args, last_call: dict[str, float]) -> dict:
    """Return the update values for one contact.

    safe -> deliverable, invalid -> undeliverable, risky/unknown -> needs review. For inferred emails the
    candidates are tried in order while Reacher says invalid; any other verdict stops the search, since
    catch-all, blocked or unreachable servers give the same answer for every candidate.
    """
    now = datetime.now(timezone.utc)
    details: dict = {"engine": "reacher", "probes": [], "checked_at": now.isoformat()}
    probes = details["probes"]

    def finish(status: str, email: str | None = None, note: str | None = None, summary: dict | None = None,
               retryable: bool = False) -> dict:
        values = {"email_checked_at": now, "email_check_details": details, "email_status": status}
        if email and email.casefold() != contact["email"].casefold():
            values["email"] = email
        summary = summary or {}
        details.update(
            needs_review=status in NEEDS_REVIEW_STATUSES,
            retryable=retryable,
            catch_all=summary.get("is_catch_all"),
            role_based=bool(summary.get("is_role_account")),
            reacher=summary or None,
        )
        if note:
            details["note"] = note
        results = {p["email"].casefold(): p for p in probes}
        values["candidate_emails"] = [
            {**item, "check": results[item["email"].casefold()]["result"]}
            if item.get("email", "").casefold() in results else item
            for item in contact["candidate_emails"] or []
        ]
        return values

    last_summary: dict | None = None
    for address in candidate_list(contact, args.max_probes):
        outcome = check_address(address, client, args.delay, last_call)
        summary, note = outcome["summary"], outcome["note"]
        probes.append({"email": address, "code": None, "message": note or "safe",
                       "result": (summary or {}).get("is_reachable") or "unknown"})
        if summary is None:  # the call failed (timeout, dropped connection)
            return finish("unknown", note=note, retryable=True)
        last_summary = summary
        if outcome["status"] == "undeliverable":
            continue  # this guess does not exist; try the next candidate
        status = outcome["status"]
        return finish(status, address if status == "deliverable" else None, note, summary, outcome["retryable"])
    note = reacher_note(last_summary) if len(probes) == 1 else f"Reacher says none of the {len(probes)} candidate addresses exist"
    return finish("undeliverable", note=note, summary=last_summary)


def was_retryable(values: dict) -> bool:
    return values["email_status"] == "unknown" and bool(values["email_check_details"].get("retryable"))


# ---------------------------------------------------------------- database and main


def load_contacts(connection, contacts, args):
    statement = (
        select(
            contacts.c.id,
            contacts.c.business_id,
            contacts.c.name,
            contacts.c.email,
            contacts.c.email_source,
            contacts.c.email_status,
            contacts.c.candidate_emails,
            contacts.c.is_primary,
        )
        .where(contacts.c.email.is_not(None), contacts.c.email != "")
        .order_by(contacts.c.is_primary.desc(), contacts.c.business_id, contacts.c.id)
    )
    if not args.recheck:
        statement = statement.where(
            or_(*(contacts.c.email_status == s for s in VERIFIABLE_STATUSES), contacts.c.email_status.is_(None))
        )
    if args.business_id is not None:
        statement = statement.where(contacts.c.business_id == args.business_id)
    if args.primary_only:
        statement = statement.where(contacts.c.is_primary.is_(True))
    if args.limit is not None:
        statement = statement.limit(args.limit)
    return [dict(row._mapping) for row in connection.execute(statement)]


def write_report(path: Path, rows: list[dict]) -> None:
    fields = ["contact_id", "business_id", "name", "original_email", "email", "status", "catch_all",
              "role_based", "probes", "note", "last_reply"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Report written to {path}")


def add_reacher_arguments(parser: argparse.ArgumentParser) -> None:
    """Options shared by every script that talks to the local Reacher container."""
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Minimum seconds between checks to one domain.")
    parser.add_argument("--reacher-url", help=f"Local Reacher base URL (default: REACHER_URL or {REACHER_DEFAULT_URL}).")
    parser.add_argument("--reacher-timeout", type=float, default=DEFAULT_REACHER_TIMEOUT,
                        help="Seconds to wait for one Reacher check (default: 90).")
    parser.add_argument("--mail-from", help="Sender address Reacher announces (default: VERIFY_MAIL_FROM).")
    parser.add_argument("--helo", help="Fully-qualified hostname or [ip] Reacher announces (default: VERIFY_HELO).")
    parser.add_argument("--no-auto-start", action="store_true",
                        help="Do not create/start/reconfigure the Reacher Docker container.")
    parser.add_argument("--reacher-container", default=REACHER_CONTAINER, help=argparse.SUPPRESS)


def prepare_reacher(parser: argparse.ArgumentParser, args) -> ReacherClient | None:
    """Validate the sender identity, make sure Reacher runs with it, and check the sender can receive mail.

    Calls parser.error() for configuration mistakes. Returns None (after printing why) when Reacher cannot be
    used, so the caller can exit with status 1.
    """
    mail_from = args.mail_from or os.environ.get("VERIFY_MAIL_FROM") or None
    helo = args.helo or os.environ.get("VERIFY_HELO") or None
    if helo and "." not in helo:
        parser.error(f"HELO name '{helo}' is not a fully-qualified hostname (e.g. mail.yourdomain.com); mail servers refuse it.")
    for label, value in (("VERIFY_MAIL_FROM", (mail_from or "@").rpartition("@")[2]), ("VERIFY_HELO", helo or "")):
        if value and is_reserved_domain(value):
            parser.error(f"{label} uses the reserved placeholder domain '{value}', which cannot receive mail; mail servers "
                         "reject checks sent from it and Reacher then reports real mailboxes as invalid. Use a domain you own.")
    if not helo and not args.no_auto_start:
        parser.error(
            "VERIFY_HELO is not set. Without it Reacher announces 'localhost' and mail servers refuse every check "
            "(results would all be 'unknown'). Set VERIFY_HELO=mail.yourdomain.com (and VERIFY_MAIL_FROM) in .env, "
            "or pass --helo/--mail-from."
        )
    if mail_from and mail_from.rpartition("@")[2].casefold() in CONSUMER_SENDER_DOMAINS:
        print(f"Warning: sender '{mail_from}' is on a consumer provider; servers check SPF for the sender and may "
              "refuse or block the checks. Use an address on a domain you control.")

    client = ReacherClient(args.reacher_url or os.environ.get("REACHER_URL") or REACHER_DEFAULT_URL, args.reacher_timeout)
    try:
        version = ensure_reacher(client, mail_from, helo, not args.no_auto_start, args.reacher_container)
    except ReacherUnavailable as error:
        print(error)
        print("Reacher must run locally in Docker (see README).")
        return None
    print(f"Using Reacher {version} at {client.base_url}. Outbound port 25 must be open for it. No email is sent.")
    if mail_from:
        problem = check_sender(client, mail_from)
        if problem:
            print(f"Sender problem: {problem}. Mail servers would reject every check and Reacher would report real "
                  "mailboxes as invalid. Set VERIFY_MAIL_FROM to an address on a domain you own that receives mail.")
            return None
    return client


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify contact emails with a self-hosted Reacher server in Docker. Sends no email."
    )
    parser.add_argument("--business-id", type=int, help="Verify one business only.")
    parser.add_argument("--limit", type=int, help="Maximum number of contacts in this run.")
    parser.add_argument("--primary-only", action="store_true", help="Only verify primary contacts.")
    parser.add_argument("--recheck", action="store_true", help="Also re-verify contacts that already have a result.")
    parser.add_argument("--max-probes", type=int, default=DEFAULT_MAX_PROBES, help="Candidate addresses to try per contact.")
    add_reacher_arguments(parser)
    parser.add_argument("--retry-rounds", type=int, default=DEFAULT_RETRY_ROUNDS,
                        help="Extra passes for greylisted/timed-out contacts (default: 1; 0 disables).")
    parser.add_argument("--retry-wait", type=float, default=DEFAULT_RETRY_WAIT,
                        help="Seconds to wait before each retry pass (default: 120).")
    parser.add_argument("--report", type=Path, help="Write a CSV report of this run's results.")
    parser.add_argument("--dry-run", action="store_true", help="Check and print results without writing to the database.")
    args = parser.parse_args()
    if args.max_probes < 1 or args.delay < 0 or args.reacher_timeout <= 0 or args.retry_rounds < 0 or args.retry_wait < 0:
        parser.error("--max-probes must be >= 1, --reacher-timeout > 0, and the delay/retry options cannot be negative")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set in the environment or project .env file")
    client = prepare_reacher(parser, args)
    if client is None:
        return 1

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    try:
        contacts = Table("business_contacts", metadata, autoload_with=engine)
        with engine.connect() as connection:
            queue = load_contacts(connection, contacts, args)
        if not queue:
            print("No contacts need email verification (use --recheck to verify again).")
            return 0

        print(f"Verifying {len(queue)} contact(s).")
        final: dict[int, dict] = {}
        report_rows: dict[int, dict] = {}
        last_call: dict[str, float] = {}
        state = {"no_smtp_streak": 0, "warned_helo": False}

        def process(contact: dict) -> dict:
            values = verify_contact(contact, client, args, last_call)
            details = values["email_check_details"]
            summary = details.get("reacher") or {}
            if summary.get("can_connect_smtp") is False:
                state["no_smtp_streak"] += 1
            elif summary.get("can_connect_smtp"):
                state["no_smtp_streak"] = 0
            if not state["warned_helo"] and "fully-qualified" in (summary.get("smtp_error") or "").casefold():
                state["warned_helo"] = True
                print("  Warning: the mail server refused Reacher's HELO name. Set VERIFY_HELO to a fully-qualified "
                      "hostname in .env and rerun (the container is reconfigured automatically).")
            chosen = values.get("email", contact["email"])
            tags = ["catch-all"] * bool(details["catch_all"]) + ["role-based"] * details["role_based"]
            review = " NEEDS REVIEW" if details["needs_review"] else ""
            tag_text = f" ({', '.join(tags)})" if tags else ""
            print(f"  [{values['email_status']}]{review} {contact['name']} <{chosen}>{tag_text} {details.get('note', '')}".rstrip())
            if not args.dry_run:
                with engine.begin() as connection:
                    connection.execute(update(contacts).where(contacts.c.id == contact["id"]).values(**values))
            report_rows[contact["id"]] = {
                "contact_id": contact["id"], "business_id": contact["business_id"], "name": contact["name"],
                "original_email": contact["email"], "email": chosen, "status": values["email_status"],
                "catch_all": details["catch_all"], "role_based": details["role_based"],
                "probes": len(details["probes"]), "note": details.get("note", ""),
                "last_reply": details["probes"][-1]["message"] if details["probes"] else "",
            }
            return values

        aborted = False
        pending = list(queue)
        for round_number in range(args.retry_rounds + 1):
            retry: list[dict] = []
            if round_number:
                print(f"Retry pass {round_number}: waiting {args.retry_wait:.0f}s for greylisting to expire...")
                time.sleep(args.retry_wait)
                state["no_smtp_streak"] = 0
            for contact in pending:
                try:
                    values = process(contact)
                except ReacherUnavailable as error:
                    print(f"{error} Stopping; rerun once Reacher is back.")
                    aborted = True
                    break
                final[contact["id"]] = values
                if was_retryable(values):
                    retry.append(contact)
                if state["no_smtp_streak"] >= MAX_CONSECUTIVE_NO_SMTP:
                    print(f"Reacher could not open an SMTP connection to {state['no_smtp_streak']} servers in a row; "
                          "outbound port 25 is probably blocked for Docker or your network. Stopping.")
                    aborted = True
                    break
            if aborted or not retry:
                break
            pending = retry

        counts: dict[str, int] = {}
        for values in final.values():
            counts[values["email_status"]] = counts.get(values["email_status"], 0) + 1
        print("Done: " + ", ".join(f"{count} {status}" for status, count in sorted(counts.items())) + ".")
        held = sum(1 for v in final.values() if v["email_status"] in NEEDS_REVIEW_STATUSES)
        if held:
            print(f"{held} contact(s) need review (risky/unknown); do not send to them automatically.")
        if args.report:
            write_report(args.report, list(report_rows.values()))
        return 1 if aborted else 0
    except SQLAlchemyError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
