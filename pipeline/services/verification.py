"""Verify candidate emails with a self-hosted Reacher server (Docker) and store the verified ones in `prospects`.

Flow: for each contact in `business_contacts`, take the contact's own `email` plus every address in
`candidate_emails`, check each with Reacher, and store the ones Reacher confirms as deliverable in `prospects`.
Every address's outcome (deliverable, undeliverable, risky, unknown) is also recorded on the contact in
`business_contacts.candidate_emails`, so reruns skip addresses already checked and nothing is lost.

If a stored prospect is rechecked and is no longer deliverable, its row is updated so it cannot stay "ready".

This step owns only the verification columns of `prospects` (`email_status`, `email_verification_provider`,
`email_verified_at`, `qualification_score` and the verification-derived `outreach_status`). Columns that later
stages fill (`outreach_priority`, `outreach_facts`, `research_summary`, `do_not_contact`, `last_contacted_at`)
are never overwritten. All verification is done by Reacher; nothing here probes mail servers itself and no email
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

from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.db.models import Case, F, Q, Value, When

from pipeline.models import BusinessContact, Prospect

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

DEFAULT_DELAY = 2.0
DEFAULT_REACHER_TIMEOUT = 90.0  # a real SMTP conversation with a slow server can take a minute
MAX_CONSECUTIVE_NO_SMTP = 3

REACHER_STATUS = {"safe": "deliverable", "invalid": "undeliverable", "risky": "risky", "unknown": "unknown"}

PROVIDER = "reacher"
# What verification alone says about outreach. Later stages may move a prospect on (e.g. to "contacted"); the
# upsert only ever rewrites these values and never touches a prospect that has moved on or is do_not_contact.
OUTREACH_STATUS = {"deliverable": "ready", "risky": "needs_review", "unknown": "needs_review", "undeliverable": "rejected"}
MANAGED_OUTREACH_STATUSES = ("pending", "ready", "needs_review", "rejected")
CONCLUSIVE_VERDICTS = {"safe", "invalid", "risky"}  # Reacher verdicts that stay as recorded; unknown is checked again


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


def add_reacher_arguments(parser) -> None:
    """Options for talking to the local Reacher container."""
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Minimum seconds between checks to one domain.")
    parser.add_argument("--reacher-url", help=f"Local Reacher base URL (default: REACHER_URL or {REACHER_DEFAULT_URL}).")
    parser.add_argument("--reacher-timeout", type=float, default=DEFAULT_REACHER_TIMEOUT,
                        help="Seconds to wait for one Reacher check (default: 90).")
    parser.add_argument("--mail-from", help="Sender address Reacher announces (default: VERIFY_MAIL_FROM).")
    parser.add_argument("--helo", help="Fully-qualified hostname or [ip] Reacher announces (default: VERIFY_HELO).")
    parser.add_argument("--no-auto-start", action="store_true",
                        help="Do not create/start/reconfigure the Reacher Docker container.")
    parser.add_argument("--reacher-container", default=REACHER_CONTAINER, help=argparse.SUPPRESS)


def prepare_reacher(options: dict) -> ReacherClient | None:
    """Validate the sender identity, make sure Reacher runs with it, and check the sender can receive mail.

    Raises CommandError for configuration mistakes. Returns None (after printing why) when Reacher cannot be
    used, so the caller can exit with status 1.
    """
    mail_from = options["mail_from"] or os.environ.get("VERIFY_MAIL_FROM") or None
    helo = options["helo"] or os.environ.get("VERIFY_HELO") or None
    if helo and "." not in helo:
        raise CommandError(f"HELO name '{helo}' is not a fully-qualified hostname (e.g. mail.yourdomain.com); mail servers refuse it.")
    for label, value in (("VERIFY_MAIL_FROM", (mail_from or "@").rpartition("@")[2]), ("VERIFY_HELO", helo or "")):
        if value and is_reserved_domain(value):
            raise CommandError(f"{label} uses the reserved placeholder domain '{value}', which cannot receive mail; mail servers "
                               "reject checks sent from it and Reacher then reports real mailboxes as invalid. Use a domain you own.")
    if not helo and not options["no_auto_start"]:
        raise CommandError(
            "VERIFY_HELO is not set. Without it Reacher announces 'localhost' and mail servers refuse every check "
            "(results would all be 'unknown'). Set VERIFY_HELO=mail.yourdomain.com (and VERIFY_MAIL_FROM) in .env, "
            "or pass --helo/--mail-from."
        )
    if mail_from and mail_from.rpartition("@")[2].casefold() in CONSUMER_SENDER_DOMAINS:
        print(f"Warning: sender '{mail_from}' is on a consumer provider; servers check SPF for the sender and may "
              "refuse or block the checks. Use an address on a domain you control.")

    client = ReacherClient(options["reacher_url"] or os.environ.get("REACHER_URL") or REACHER_DEFAULT_URL, options["reacher_timeout"])
    try:
        version = ensure_reacher(client, mail_from, helo, not options["no_auto_start"], options["reacher_container"])
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


def candidate_addresses(contact: dict, max_candidates: int = 0) -> list[str]:
    """The contact's own email first, then candidate_emails; de-duplicated, order kept (0 = no cap)."""
    entries = [contact.get("email")] + [item.get("email") for item in contact.get("candidate_emails") or []]
    seen, ordered = set(), []
    for entry in entries:
        address = entry.strip() if isinstance(entry, str) else ""
        if "@" not in address or address.casefold() in seen:
            continue
        seen.add(address.casefold())
        ordered.append(address)
    return ordered[:max_candidates] if max_candidates else ordered


def domain_shortcut(outcome: dict) -> tuple[str, str] | None:
    """(status, reason) when this outcome means every other address on the same domain would get the same answer."""
    summary = outcome["summary"]
    if summary is None:
        return "unknown", "the mail server did not answer"
    if summary.get("is_catch_all"):
        return "risky", "the domain accepts mail for any address (catch-all)"
    if summary.get("can_connect_smtp") is False:
        return "unknown", "Reacher could not connect to the mail server"
    if outcome["status"] == "unknown" and not outcome["retryable"]:
        return "unknown", "the mail server refused the checks"
    return None


def record_check(contact: dict, address: str, outcome: dict, when: datetime) -> dict:
    """Merge one outcome into the contact's candidate_emails (in place) and return the columns to update.

    Every checked address keeps Reacher's verdict (`check`) and time. If the address is the contact's own email,
    the contact's email_status/email_checked_at are updated too (never `email` itself).
    """
    stamp = when.isoformat()
    for item in contact.get("candidate_emails") or []:
        if isinstance(item, dict) and str(item.get("email", "")).strip().casefold() == address.casefold():
            item["check"] = outcome["verdict"]
            item["checked_at"] = stamp
    values = {"candidate_emails": contact.get("candidate_emails") or []}
    if address.casefold() == (contact.get("email") or "").strip().casefold():
        values.update(email_status=outcome["status"], email_checked_at=when)
    return values


def already_decided(contact: dict, address: str, prospect_status: str | None) -> bool:
    """True when this address needs no new check: it is a stored deliverable prospect, or was already found
    undeliverable/risky. A deliverable verdict is only trusted through the prospects table."""
    if prospect_status == "deliverable":
        return True
    key = address.casefold()
    for item in contact.get("candidate_emails") or []:
        if isinstance(item, dict) and str(item.get("email", "")).strip().casefold() == key:
            if item.get("check") in CONCLUSIVE_VERDICTS and item.get("check") != "safe":
                return True
    return key == (contact.get("email") or "").strip().casefold() and contact.get("email_status") in {"undeliverable", "risky"}


def prospect_values(contact: dict, address: str, status: str, now: datetime) -> dict:
    return {
        "business_id": contact["business_id"],
        "contact_id": contact["id"],
        "email": address,
        "email_status": status,
        "email_verification_provider": PROVIDER,
        "email_verified_at": now,
        "qualification_score": contact["qualification_score"],
        "outreach_status": OUTREACH_STATUS[status],
    }


def movable_outreach() -> Q:
    """Prospects whose outreach_status verification may still change (not moved on by a later stage, not do-not-contact)."""
    return Q(outreach_status__in=MANAGED_OUTREACH_STATUSES, do_not_contact=False)


def upsert_prospect(values: dict) -> None:
    """Insert the prospect, or refresh only the verification columns of an existing one."""
    with transaction.atomic():
        prospect, created = Prospect.objects.select_for_update().get_or_create(
            contact_id=values["contact_id"], email=values["email"], defaults=values
        )
        if created:
            return
        fields = ["email_status", "email_verification_provider", "email_verified_at", "qualification_score"]
        for field in fields:
            setattr(prospect, field, values[field])
        if prospect.outreach_status in MANAGED_OUTREACH_STATUSES and not prospect.do_not_contact:
            prospect.outreach_status = values["outreach_status"]
            fields.append("outreach_status")
        prospect.save(update_fields=[*fields, "updated_at"])


def update_existing_prospect(contact_id: int, address: str, status: str, now: datetime) -> None:
    """A stored prospect was rechecked and is no longer deliverable: reflect it, without touching later-stage columns."""
    Prospect.objects.filter(contact_id=contact_id, email=address).update(
        email_status=status,
        email_verified_at=now,
        outreach_status=Case(When(movable_outreach(), then=Value(OUTREACH_STATUS[status])), default=F("outreach_status")),
    )


def load_contacts(options: dict) -> list[dict]:
    queryset = BusinessContact.objects.order_by("business_id", "-is_primary", "id")
    if options["business_id"]:
        queryset = queryset.filter(business_id__in=options["business_id"])
    if options.get("contact_id"):
        queryset = queryset.filter(id__in=options["contact_id"])
    if options["primary_only"]:
        queryset = queryset.filter(is_primary=True)
    if options["min_score"] is not None:
        queryset = queryset.filter(business__website_profile__qualification_score__gte=options["min_score"])
    if options["limit"] is not None:
        queryset = queryset[: options["limit"]]
    return list(
        queryset.values(
            "id", "business_id", "name", "email", "email_status", "candidate_emails", "is_primary",
            qualification_score=F("business__website_profile__qualification_score"),
        )
    )


def existing_statuses(contact_ids: list[int]) -> dict[tuple[int, str], str | None]:
    if not contact_ids:
        return {}
    rows = Prospect.objects.filter(contact_id__in=contact_ids).values_list("contact_id", "email", "email_status")
    return {(contact_id, email): status for contact_id, email, status in rows}


def write_report(path: Path, rows: list[dict]) -> None:
    fields = ["contact_id", "business_id", "email", "status", "verdict", "probed", "catch_all", "role_account", "note"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Report written to {path}")


def add_arguments(parser) -> None:
    parser.add_argument("--business-id", type=int, action="append", help="Process this business only (repeatable).")
    parser.add_argument("--contact-id", type=int, action="append", help="Process this contact only (repeatable).")
    parser.add_argument("--limit", type=int, help="Maximum number of contacts in this run.")
    parser.add_argument("--primary-only", action="store_true", help="Only process primary contacts.")
    parser.add_argument("--min-score", type=int, help="Only businesses with at least this qualification score.")
    parser.add_argument("--recheck", action="store_true", help="Check again addresses that were already checked.")
    parser.add_argument("--max-candidates", type=int, default=0, help="Cap on addresses checked per contact (0 = all).")
    parser.add_argument("--probe-all", action="store_true",
                        help="Check every address even after the domain is known to be catch-all or unreachable.")
    add_reacher_arguments(parser)
    parser.add_argument("--report", type=Path, help="Write a CSV report (with reasons) of this run's results.")
    parser.add_argument("--dry-run", action="store_true", help="Check and print results without writing to the database.")


def run(options: dict) -> int:
    if options["delay"] < 0 or options["reacher_timeout"] <= 0 or options["max_candidates"] < 0:
        raise CommandError("--delay and --max-candidates cannot be negative and --reacher-timeout must be > 0")
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    client = prepare_reacher(options)
    if client is None:
        return 1

    dry_run = options["dry_run"]
    try:
        queue = load_contacts(options)
        known = existing_statuses([c["id"] for c in queue])
        if not queue:
            print("No contacts to process.")
            return 0

        print(f"Checking candidate emails for {len(queue)} contact(s).")
        last_call: dict[str, float] = {}
        domain_state: dict[str, tuple[str, str]] = {}
        counts: dict[str, int] = {}
        report: list[dict] = []
        skipped = streak = prospect_count = 0
        aborted = False
        for contact in queue:
            addresses = candidate_addresses(contact, options["max_candidates"])
            print(f"Contact {contact['id']} ({contact['name']}), business {contact['business_id']}: {len(addresses)} address(es)")
            for address in addresses:
                prospect_status = known.get((contact["id"], address))
                if not options["recheck"] and already_decided(contact, address, prospect_status):
                    skipped += 1
                    continue
                domain = address.rpartition("@")[2].casefold()
                if domain in domain_state and not options["probe_all"]:
                    status, reason = domain_state[domain]
                    outcome = {"status": status, "verdict": status, "summary": None, "note": f"not checked: {reason}"}
                    probed = False
                else:
                    try:
                        outcome = check_address(address, client, options["delay"], last_call)
                    except ReacherUnavailable as error:
                        print(f"{error} Stopping; rerun once Reacher is back. Results so far are saved.")
                        aborted = True
                        break
                    probed = True
                    shortcut = domain_shortcut(outcome)
                    if shortcut and not options["probe_all"]:
                        domain_state[domain] = shortcut
                    summary = outcome["summary"] or {}
                    if summary.get("can_connect_smtp") is False:
                        streak += 1
                    elif summary.get("can_connect_smtp"):
                        streak = 0
                status = outcome["status"]
                counts[status] = counts.get(status, 0) + 1
                summary = outcome["summary"] or {}
                note = outcome["note"] or ""
                now = datetime.now(timezone.utc)
                stored = ""
                if status == "deliverable":
                    stored = " -> prospect"
                elif prospect_status is not None:
                    stored = " -> existing prospect updated"
                print(f"  [{status}] <{address}>{' (not checked)' if not probed else ''}{stored} {note}".rstrip())
                if not dry_run:
                    with transaction.atomic():
                        BusinessContact.objects.filter(pk=contact["id"]).update(**record_check(contact, address, outcome, now))
                        if status == "deliverable":
                            upsert_prospect(prospect_values(contact, address, status, now))
                        elif prospect_status is not None:
                            update_existing_prospect(contact["id"], address, status, now)
                else:
                    record_check(contact, address, outcome, now)  # keep the in-memory view consistent within the run
                if status == "deliverable":
                    prospect_count += 1
                known[(contact["id"], address)] = status if (status == "deliverable" or prospect_status is not None) else prospect_status
                report.append({
                    "contact_id": contact["id"], "business_id": contact["business_id"], "email": address,
                    "status": status, "verdict": outcome["verdict"], "probed": probed,
                    "catch_all": summary.get("is_catch_all"), "role_account": summary.get("is_role_account"), "note": note,
                })
                if streak >= MAX_CONSECUTIVE_NO_SMTP:
                    print(f"Reacher could not open an SMTP connection to {streak} servers in a row; outbound port 25 is "
                          "probably blocked for Docker or your network. Stopping. Results so far are saved.")
                    aborted = True
                    break
            if aborted:
                break

        total = sum(counts.values())
        verb = "would be" if dry_run else "added or updated as"
        print(f"Done: {total} address(es) checked ("
              + (", ".join(f"{n} {st}" for st, n in sorted(counts.items())) if counts else "none")
              + f"); {prospect_count} {verb} prospects"
              + (f"; {skipped} skipped (already checked)" if skipped else "") + ".")
        needs_review = counts.get("risky", 0) + counts.get("unknown", 0)
        if needs_review:
            print(f"{needs_review} address(es) need review (risky/unknown); they are not prospects and must not be emailed automatically.")
        if options["report"]:
            write_report(options["report"], report)
        return 1 if aborted else 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `python manage.py migrate` first.")
        return 1
