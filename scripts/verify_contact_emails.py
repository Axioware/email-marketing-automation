import argparse
import csv
import os
import re
import secrets
import smtplib
import socket
import ssl
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import dns.exception
import dns.resolver
from dotenv import load_dotenv
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import MetaData, Table, create_engine, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

SMTP_PORT = 25
PREFLIGHT_HOST = "gmail-smtp-in.l.google.com"
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_PROBES = 6
DEFAULT_PROBE_DELAY = 2.0
DEFAULT_RETRY_ROUNDS = 1
DEFAULT_RETRY_WAIT = 120.0
MAX_MX_HOSTS_TRIED = 3
MAX_CONNECTION_FAILURES_BEFORE_ABORT = 3
VERIFIABLE_STATUSES = ("unverified", "unknown")

REACHER_DEFAULT_URL = "http://127.0.0.1:8080"
REACHER_CHECK_PATHS = ("/v1/check_email", "/v0/check_email")  # current image first; the bare /check_email is a 404
DEFAULT_REACHER_TIMEOUT = 90.0  # a real SMTP conversation with a slow server can take a minute
MAX_CONSECUTIVE_NO_SMTP = 3
REACHER_STATUS = {"safe": "deliverable", "invalid": "undeliverable", "risky": "risky", "unknown": "unknown"}
NEEDS_REVIEW_STATUSES = {"risky", "unknown"}  # held for review, never used automatically

# Mail gateways front the real mailbox server and usually accept every recipient.
GATEWAY_MX_MARKERS = (
    "mimecast", "pphosted", "proofpoint", "barracuda", "messagelabs", "mailcontrol", "fireeyecloud",
    "iphmx", "trendmicro", "sophos", "forcepoint", "spamh.com", "mxthunder", "antispamcloud", "cloudfilter",
    "spamexperts", "mailanyone", "emailsrvr-filter", "securence", "reflexion",
)
# (provider, MX hostname fragments, SPF include fragments)
PROVIDERS = (
    ("google", ("google.com", "googlemail.com"), ("_spf.google.com", "googlehosted.com")),
    ("microsoft", ("protection.outlook.com", "outlook.com", "hotmail.com"), ("spf.protection.outlook.com",)),
    ("zoho", ("zoho.com", "zoho.eu", "zoho.in", "zohomail"), ("zoho.com", "zoho.eu", "zoho.in")),
    ("hostinger", ("hostinger",), ("hostinger",)),
    ("godaddy", ("secureserver.net",), ("secureserver.net",)),
    ("namecheap", ("registrar-servers.com", "privateemail.com"), ("privateemail.com", "registrar-servers.com")),
    ("yahoo", ("yahoodns.net", "yahoo.com"), ("yahoo.com",)),
    ("fastmail", ("messagingengine.com",), ("messagingengine.com",)),
    ("rackspace", ("emailsrvr.com",), ("emailsrvr.com",)),
    ("ionos", ("ionos.", "1and1", "kundenserver.de"), ("ionos.", "1and1")),
    ("bluehost", ("bluehost.com", "hostmonster.com"), ("bluehost.com",)),
    ("hostgator", ("hostgator.com",), ("hostgator.com",)),
    ("cpanel_host", ("mail.", "mx."), ()),  # generic; only used as a last resort label
)
# Providers that throttle or flag probing hardest get a longer minimum gap between probes (seconds).
PROVIDER_MIN_DELAY = {"google": 4.0, "microsoft": 5.0, "yahoo": 8.0, "gateway": 5.0}
CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "hotmail.com", "hotmail.co.uk",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com", "zoho.com", "yandex.com", "yandex.ru",
}
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "sharklasers.com",
    "grr.la", "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org", "temp-mail.io",
    "throwawaymail.com", "yopmail.com", "yopmail.net", "trashmail.com", "trashmail.net", "getnada.com",
    "nada.email", "maildrop.cc", "dispostable.com", "fakeinbox.com", "mailnesia.com", "mintemail.com",
    "mytemp.email", "spamgourmet.com", "tempinbox.com", "tempmailo.com", "burnermail.io", "mohmal.com",
    "emailondeck.com", "moakt.com", "mailcatch.com", "inboxkitten.com", "discard.email", "tmpmail.org",
    "tmpmail.net", "33mail.com", "anonbox.net", "spambox.us", "harakirimail.com", "mailforspam.com",
    "owlymail.com", "luxusmail.org", "crazymailing.com", "emailfake.com", "fakemailgenerator.com",
    "mailpoof.com", "tempr.email", "minuteinbox.com", "dropmail.me", "1secmail.com", "1secmail.org",
}
ROLE_BASED_LOCAL_PARTS = {
    "info", "contact", "admin", "office", "hello", "hi", "support", "sales", "appointments", "appointment",
    "reception", "frontdesk", "booking", "bookings", "enquiries", "enquiry", "inquiries", "inquiry", "mail",
    "team", "billing", "hr", "careers", "service", "services", "care", "help", "dr", "doctor", "clinic",
    "dental", "webmaster", "marketing", "accounts", "noreply", "no-reply", "feedback", "postmaster",
    "abuse", "privacy", "legal", "press", "media", "jobs",
}
USER_UNKNOWN_MARKERS = (
    "5.1.1", "5.1.10", "5.1.2", "5.4.1", "user unknown", "no such user", "does not exist", "unknown user",
    "recipient not found", "invalid recipient", "mailbox unavailable", "mailbox not found", "no mailbox",
    "recipient rejected", "address rejected", "user not found", "not a valid mailbox", "unrouteable address",
)
# Replies about OUR connection (greeting, sender, SPF), not about the recipient. Never evidence the mailbox is missing.
CONFIG_MARKERS = (
    "helo", "ehlo", "fully-qualified", "fully qualified", "hostname", "5.5.1", "5.5.2", "5.5.4", "5.1.7", "5.1.8",
    "sender", "spf", "from address", "mail from", "domain of", "authentication", "must issue", "starttls",
)
BLOCKED_MARKERS = (
    "5.7.", "blocked", "blacklist", "blocklist", "spamhaus", "reputation", "policy", "banned", "not permitted",
    "denied by", "relay", "service unavailable", "access denied", "rate limit", "too many", "dnsbl",
)
_PROBE_TLS_CONTEXT = ssl.create_default_context()  # verifying; a failed handshake falls back to plain SMTP


class SmtpUnreachable(RuntimeError):
    pass


# ---------------------------------------------------------------- DNS and provider


def lookup_mx(domain: str, resolver: dns.resolver.Resolver) -> list[str] | None:
    """MX hosts by priority. Falls back to the domain itself when it has an address but no MX.

    Returns None when the domain cannot receive mail at all (no MX and no A/AAAA, or a null MX).
    """
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = [str(rdata.exchange).rstrip(".") for rdata in sorted(answers, key=lambda r: r.preference)]
        return [host for host in hosts if host] or None  # a null MX (".") means "does not accept mail"
    except dns.resolver.NoAnswer:
        pass
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout):
        return None
    for record in ("A", "AAAA"):
        try:
            resolver.resolve(domain, record)
            return [domain]
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout):
            continue
    return None


def lookup_txt(name: str, resolver: dns.resolver.Resolver) -> list[str]:
    try:
        return [b"".join(rdata.strings).decode("utf-8", "replace") for rdata in resolver.resolve(name, "TXT")]
    except (dns.exception.DNSException, OSError):
        return []


def detect_provider(mx_hosts: list[str], spf: str | None = None) -> str:
    """Name the mail provider from MX hosts; refine an unrecognised one from SPF includes."""
    joined = " ".join(mx_hosts).casefold()
    if any(marker in joined for marker in GATEWAY_MX_MARKERS):
        return "gateway"
    for provider, mx_markers, _ in PROVIDERS:
        if provider != "cpanel_host" and any(marker in joined for marker in mx_markers):
            return provider
    if spf:
        spf_text = spf.casefold()
        for provider, _, spf_markers in PROVIDERS:
            if any(marker in spf_text for marker in spf_markers):
                return provider
    return "other"


# ---------------------------------------------------------------- SMTP probing


def classify_reply(code: int, message: str) -> str:
    """Map an SMTP reply to accepted / rejected / full / blocked / temporary."""
    text = message.casefold()
    if code in (250, 251):
        return "accepted"
    if code == 552 or "mailbox full" in text or "over quota" in text or "5.2.2" in text:
        return "full"  # the mailbox exists but cannot take mail
    if 400 <= code < 500:
        return "temporary"  # greylisting, rate limits, "try again later"
    if code >= 500:
        if code in (500, 501, 503, 504) or any(marker in text for marker in CONFIG_MARKERS):
            return "blocked"  # our greeting/sender was refused; says nothing about the recipient
        if any(marker in text for marker in USER_UNKNOWN_MARKERS):
            return "rejected"
        if any(marker in text for marker in BLOCKED_MARKERS):
            return "blocked"
        return "rejected"  # a plain 550/553 on RCPT with no other explanation
    return "temporary"


def _text(message) -> str:
    return (message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message))[:300]


class SmtpSession:
    """One connection to a domain's mail server, reused for several RCPT probes. No message is ever sent."""

    def __init__(self, mx_hosts: list[str], helo: str, mail_from: str, timeout: float, port: int = SMTP_PORT):
        self.mx_hosts = mx_hosts
        self.helo = helo
        self.mail_from = mail_from
        self.timeout = timeout
        self.port = port
        self.server: smtplib.SMTP | None = None
        self.connected_host: str | None = None
        self.use_tls = True

    def _open(self, host: str) -> smtplib.SMTP:
        server = smtplib.SMTP(timeout=self.timeout, local_hostname=self.helo)
        server._host = host  # smtplib only sets this when connecting from the constructor; STARTTLS needs it
        try:
            server.connect(host, self.port)
            code, _ = server.ehlo()
            if code >= 400:
                server.helo()
            elif self.use_tls and server.has_extn("starttls"):
                server.starttls(context=_PROBE_TLS_CONTEXT)
                server.ehlo()
            return server
        except BaseException:
            try:
                server.close()
            except OSError:
                pass
            raise

    def _connect(self) -> None:
        last_error: Exception | None = None
        for host in self.mx_hosts[:MAX_MX_HOSTS_TRIED]:
            for attempt in range(2):
                try:
                    self.server, self.connected_host = self._open(host), host
                    return
                except (ssl.SSLError, ValueError, smtplib.SMTPNotSupportedError) as error:
                    last_error = error
                    self.use_tls = False  # server mishandles STARTTLS; retry this host in plain text
                except (OSError, smtplib.SMTPException) as error:
                    last_error = error
                    break
        raise SmtpUnreachable(f"{type(last_error).__name__}: {last_error}")

    def close(self) -> None:
        if self.server is not None:
            try:
                self.server.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self.server = None

    def probe(self, address: str) -> dict:
        """Ask the server whether it would accept mail for the address, then reset. Retries once if dropped."""
        for attempt in range(2):
            try:
                if self.server is None:
                    self._connect()
                code, message = self.server.mail(self.mail_from)
                if code >= 400:
                    return {"email": address, "code": code, "message": _text(message), "result": "blocked"}
                code, message = self.server.rcpt(address)
                try:
                    self.server.rset()
                except (smtplib.SMTPException, OSError):
                    self.close()
                text = _text(message)
                return {"email": address, "code": code, "message": text, "result": classify_reply(code, text)}
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout, TimeoutError, ConnectionError):
                self.close()
                if attempt:
                    return {"email": address, "code": None, "message": "connection dropped", "result": "temporary"}
            except smtplib.SMTPResponseException as error:
                self.close()
                text = _text(error.smtp_error)
                return {"email": address, "code": error.smtp_code, "message": text,
                        "result": classify_reply(error.smtp_code, text)}
            except (smtplib.SMTPException, OSError) as error:
                self.close()
                return {"email": address, "code": None, "message": f"{type(error).__name__}: {error}"[:200],
                        "result": "temporary"}
        return {"email": address, "code": None, "message": "no response", "result": "temporary"}


class DomainInfo:
    """Everything learned about one mail domain, computed once per run."""

    def __init__(self, domain: str, mx_hosts: list[str] | None, provider: str, spf: str | None, dmarc: bool):
        self.domain = domain
        self.mx_hosts = mx_hosts
        self.provider = provider
        self.spf = spf
        self.dmarc = dmarc
        self.consumer = domain in CONSUMER_DOMAINS
        self.disposable = domain in DISPOSABLE_DOMAINS
        self.catch_all: bool | None = None  # None until tested, or if the test was inconclusive
        self.catch_all_tested = False
        self.session: SmtpSession | None = None
        self.unreachable = False
        self.last_probe_at = 0.0
        self.delay = 0.0

    def details(self) -> dict:
        return {
            "domain": self.domain,
            "mx": (self.mx_hosts or [])[:3],
            "provider": self.provider,
            "catch_all": self.catch_all,
            "spf": bool(self.spf),
            "dmarc": self.dmarc,
            "consumer_provider": self.consumer,
            "disposable": self.disposable,
        }


def inspect_domain(domain: str, resolver, args, helo: str, mail_from: str) -> DomainInfo:
    mx_hosts = lookup_mx(domain, resolver)
    spf = next((txt for txt in lookup_txt(domain, resolver) if txt.casefold().startswith("v=spf1")), None)
    dmarc = any(txt.casefold().startswith("v=dmarc1") for txt in lookup_txt(f"_dmarc.{domain}", resolver))
    provider = detect_provider(mx_hosts, spf) if mx_hosts else "none"
    info = DomainInfo(domain, mx_hosts, provider, spf, dmarc)
    info.delay = max(args.delay, PROVIDER_MIN_DELAY.get(provider, 0.0))
    if mx_hosts:
        info.session = SmtpSession(mx_hosts, helo, mail_from, args.timeout, args.smtp_port)
    return info


def throttled_probe(info: DomainInfo, address: str) -> dict:
    wait = info.delay - (time.monotonic() - info.last_probe_at)
    if wait > 0:
        time.sleep(wait)
    try:
        return info.session.probe(address)
    finally:
        info.last_probe_at = time.monotonic()


def test_catch_all(info: DomainInfo) -> None:
    """A random mailbox cannot exist. If the server accepts it, the server accepts everything."""
    result = throttled_probe(info, f"zq{secrets.token_hex(6)}@{info.domain}")
    if result["result"] == "accepted":
        info.catch_all = True
    elif result["result"] in ("rejected", "full"):
        info.catch_all = False
    # blocked / temporary: leave None (inconclusive)


# ---------------------------------------------------------------- verification of one contact


def local_checks(address: str) -> str | None:
    """Return an error string when the address is syntactically unusable."""
    try:
        validate_email(address, check_deliverability=False)
    except EmailNotValidError as error:
        return str(error)
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


def verify_contact(contact: dict, info: DomainInfo, args) -> dict:
    """Return the update values for one contact. Never sends a message."""
    now = datetime.now(timezone.utc)
    base = {"email_checked_at": now, "email_check_details": {**info.details(), "probes": [], "checked_at": now.isoformat()}}
    probes = base["email_check_details"]["probes"]

    def finish(status: str, email: str | None = None, note: str | None = None) -> dict:
        values = {**base, "email_status": status}
        details = values["email_check_details"]
        details.update(info.details())
        if email and email.casefold() != contact["email"].casefold():
            values["email"] = email
        if note:
            details["note"] = note
        final = values.get("email") or contact["email"]
        details["role_based"] = final.partition("@")[0].casefold() in ROLE_BASED_LOCAL_PARTS
        results = {p["email"].casefold(): p for p in probes}
        merged = []
        for item in contact["candidate_emails"] or []:
            probe = results.get(item.get("email", "").casefold())
            merged.append({**item, "smtp_code": probe["code"], "check": probe["result"]} if probe else item)
        values["candidate_emails"] = merged
        return values

    error = local_checks(contact["email"])
    if error:
        return finish("undeliverable", note=f"invalid syntax: {error}")
    if info.disposable:
        return finish("undeliverable", note="disposable email domain")
    if not info.mx_hosts:
        return finish("undeliverable", note="domain has no mail server (no MX or address record)")
    if info.consumer:
        return finish("risky", note="consumer mailbox provider; probing is unreliable and gets blocked")
    if info.unreachable:
        return finish("unknown", note="mail server unreachable (port 25 blocked or server down)")

    if not info.catch_all_tested:
        info.catch_all_tested = True
        try:
            test_catch_all(info)
        except SmtpUnreachable as error:
            info.unreachable = True
            return finish("unknown", note=f"mail server unreachable: {error}")
    if info.catch_all:
        return finish("risky", note="domain accepts mail for any address (catch-all); existence cannot be proven")

    saw_temporary = saw_blocked = False
    accepted = None
    for address in candidate_list(contact, args.max_probes):
        try:
            probe = throttled_probe(info, address)
        except SmtpUnreachable as error:
            info.unreachable = True
            return finish("unknown", note=f"mail server unreachable: {error}")
        probes.append(probe)
        if probe["result"] in ("accepted", "full"):
            accepted = probe
            break
        saw_temporary |= probe["result"] == "temporary"
        saw_blocked |= probe["result"] == "blocked"

    if accepted:
        if accepted["result"] == "full":
            return finish("risky", accepted["email"], "mailbox exists but is full")
        if info.provider == "gateway":
            return finish("risky", accepted["email"], "mail gateway in front of the mailbox; acceptance is not proof")
        if info.catch_all is None:
            return finish("risky", accepted["email"], "accepted, but the catch-all test was inconclusive")
        return finish("deliverable", accepted["email"])
    if saw_blocked:
        reply = next((p["message"] for p in reversed(probes) if p["result"] == "blocked"), "")
        return finish("unknown", note=f"server refused the probe, not the mailbox ({reply[:120]}); fix HELO/sender or try another network")
    if saw_temporary:
        return finish("unknown", note="server greylisted or rate-limited the probes; retry later")
    return finish("undeliverable", note="server rejected every candidate address")


# ---------------------------------------------------------------- Reacher engine


class ReacherUnavailable(RuntimeError):
    """The Reacher server itself cannot be used (not running, refused the secret). Stops the whole run."""


class ReacherError(RuntimeError):
    """One check failed (timeout, server error). The contact is marked unknown and can be retried."""


class ReacherClient:
    """Minimal client for the reacherhq/backend HTTP API (https://github.com/reacherhq/check-if-email-exists)."""

    def __init__(self, base_url: str, timeout: float, secret: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.secret = secret
        self.path: str | None = None  # remembered after the first successful call

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.secret:
            headers["x-reacher-secret"] = self.secret
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with urlopen(request, timeout=timeout or self.timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def version(self) -> str:
        """Health check. Raises ReacherUnavailable when the server cannot be reached."""
        try:
            return str(self._request("GET", "/version", timeout=5).get("version", "unknown"))
        except HTTPError as error:
            raise ReacherUnavailable(f"Reacher at {self.base_url} answered HTTP {error.code} to /version.") from error
        except (URLError, OSError, ValueError) as error:
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
                if error.code in (401, 403):
                    raise ReacherUnavailable("Reacher rejected the request; check REACHER_API_SECRET.") from error
                raise ReacherError(f"Reacher returned HTTP {error.code}") from error
            except (socket.timeout, TimeoutError) as error:
                raise ReacherError("Reacher request timed out") from error
            except URLError as error:
                if isinstance(error.reason, (socket.timeout, TimeoutError)):
                    raise ReacherError("Reacher request timed out") from error
                raise ReacherUnavailable(f"Lost connection to Reacher at {self.base_url} ({type(error.reason).__name__}).") from error
            except ValueError as error:
                raise ReacherError("Reacher returned an unreadable response") from error
        raise ReacherError("Reacher exposes no check_email endpoint")


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


def verify_contact_reacher(contact: dict, client: ReacherClient, args, last_call: dict[str, float]) -> dict:
    """Return the update values for one contact using Reacher. Never sends a message.

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
        details.update(
            needs_review=status in NEEDS_REVIEW_STATUSES,
            retryable=retryable,
            catch_all=(summary or {}).get("is_catch_all"),
            role_based=(summary or {}).get("is_role_account")
            if (summary or {}).get("is_role_account") is not None
            else (values.get("email") or contact["email"]).partition("@")[0].casefold() in ROLE_BASED_LOCAL_PARTS,
            reacher=summary,
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

    error = local_checks(contact["email"])
    if error:
        return finish("undeliverable", note=f"invalid syntax: {error}")

    last_summary: dict | None = None
    for address in candidate_list(contact, args.max_probes):
        domain = address.rpartition("@")[2].casefold()
        wait = args.delay - (time.monotonic() - last_call.get(domain, 0.0))
        if wait > 0:
            time.sleep(wait)
        try:
            result = client.check(address)
        except ReacherError as error:
            probes.append({"email": address, "code": None, "message": str(error), "result": "unknown"})
            return finish("unknown", note=str(error), retryable=True)
        finally:
            last_call[domain] = time.monotonic()
        summary = summarize_reacher(result)
        status = REACHER_STATUS.get(summary["is_reachable"], "unknown")
        note = reacher_note(summary)
        probes.append({"email": address, "code": None, "message": note or "safe", "result": summary["is_reachable"]})
        last_summary = summary
        if status == "undeliverable":
            continue  # this guess does not exist; try the next candidate
        retryable = status == "unknown" and not (summary["smtp_error"] or "").startswith("permanent")
        return finish(status, address if status == "deliverable" else None, note, summary, retryable)
    note = reacher_note(last_summary) if len(probes) == 1 else f"Reacher says none of the {len(probes)} candidate addresses exist"
    return finish("undeliverable", note=note, summary=last_summary)


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
        .where(contacts.c.email.is_not(None))
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


def port_25_open(host: str, timeout: float) -> bool:
    try:
        with socket.create_connection((host, SMTP_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def was_greylisted(values: dict) -> bool:
    details = values["email_check_details"]
    return values["email_status"] == "unknown" and (
        details.get("retryable") or any(p["result"] == "temporary" for p in details["probes"])
    )


def write_report(path: Path, rows: list[dict]) -> None:
    fields = ["contact_id", "business_id", "name", "original_email", "email", "status", "provider",
              "catch_all", "role_based", "probes", "note", "last_reply"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Report written to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify contact emails with a self-hosted Reacher server (default) or the built-in SMTP prober "
        "(syntax, MX, provider, catch-all detection, RCPT probing). Sends no email."
    )
    parser.add_argument("--engine", choices=("reacher", "smtp"), default="reacher",
                        help="Verification engine (default: reacher; needs its Docker container running).")
    parser.add_argument("--reacher-url", help=f"Reacher base URL (default: REACHER_URL or {REACHER_DEFAULT_URL}).")
    parser.add_argument("--reacher-timeout", type=float, default=DEFAULT_REACHER_TIMEOUT,
                        help="Seconds to wait for one Reacher check (default: 90).")
    parser.add_argument("--business-id", type=int, help="Verify one business only.")
    parser.add_argument("--limit", type=int, help="Maximum number of contacts in this run.")
    parser.add_argument("--primary-only", action="store_true", help="Only verify primary contacts.")
    parser.add_argument("--recheck", action="store_true", help="Also re-verify contacts that already have a result.")
    parser.add_argument("--max-probes", type=int, default=DEFAULT_MAX_PROBES, help="Candidate addresses to try per contact.")
    parser.add_argument("--delay", type=float, default=DEFAULT_PROBE_DELAY, help="Minimum seconds between probes to one domain.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="SMTP/DNS timeout in seconds (smtp engine).")
    parser.add_argument("--retry-rounds", type=int, default=DEFAULT_RETRY_ROUNDS,
                        help="Extra passes for greylisted contacts (default: 1; 0 disables).")
    parser.add_argument("--retry-wait", type=float, default=DEFAULT_RETRY_WAIT,
                        help="Seconds to wait before each retry pass (default: 120).")
    parser.add_argument("--helo", help="Hostname announced to mail servers (smtp engine; default: VERIFY_HELO or this host's FQDN).")
    parser.add_argument("--mail-from", help="Sender address used in the probe (smtp engine; default: VERIFY_MAIL_FROM).")
    parser.add_argument("--report", type=Path, help="Write a CSV report of this run's results.")
    parser.add_argument("--skip-preflight", action="store_true", help="Do not test outbound port 25 first.")
    parser.add_argument("--smtp-port", type=int, default=SMTP_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Probe and print results without writing to the database.")
    args = parser.parse_args()
    if (args.max_probes < 1 or args.delay < 0 or args.timeout <= 0 or args.reacher_timeout <= 0
            or args.retry_rounds < 0 or args.retry_wait < 0):
        parser.error("--max-probes must be >= 1, timeouts must be > 0, and the delay/retry options cannot be negative")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set in the environment or project .env file")
    mail_from = helo = resolver = client = None
    if args.engine == "reacher":
        client = ReacherClient(
            args.reacher_url or os.environ.get("REACHER_URL") or REACHER_DEFAULT_URL,
            args.reacher_timeout,
            os.environ.get("REACHER_API_SECRET") or None,
        )
        try:
            print(f"Using Reacher {client.version()} at {client.base_url}. Outbound port 25 must be open for it.")
        except ReacherUnavailable as error:
            print(error)
            print("Start it with the command in the README (docker run ... reacherhq/backend), or use --engine smtp.")
            return 1
    else:
        mail_from = args.mail_from or os.environ.get("VERIFY_MAIL_FROM")
        helo = args.helo or os.environ.get("VERIFY_HELO") or socket.getfqdn()
        if not mail_from:
            if "." not in helo:
                parser.error(
                    "Set VERIFY_MAIL_FROM (e.g. verify@yourdomain.com) in .env or pass --mail-from. Mail servers "
                    "reject probes from senders on domains that do not exist."
                )
            mail_from = f"verify@{helo}"
        if "." not in helo:
            parser.error(
                f"HELO name '{helo}' is not a fully-qualified hostname (e.g. host.yourdomain.com); mail servers such as "
                "Hostinger refuse it, so every probe would fail. Set VERIFY_HELO in .env or pass --helo."
            )

        resolver = dns.resolver.Resolver()
        resolver.lifetime = args.timeout
        sender_domain = mail_from.rpartition("@")[2].casefold()
        if sender_domain in CONSUMER_DOMAINS:
            print(
                f"Warning: sender domain '{sender_domain}' is a consumer provider. Servers check SPF for the sender, "
                "and your IP is not authorised to send as that domain, so some will reject or block the probes. "
                "Use an address on a domain you control."
            )
        if lookup_mx(sender_domain, resolver) is None:
            print(f"Warning: sender domain '{sender_domain}' has no mail records; many servers will reject the probes.")
        if not args.skip_preflight and args.smtp_port == SMTP_PORT and not port_25_open(PREFLIGHT_HOST, args.timeout):
            print("Outbound port 25 is blocked on this network, so SMTP verification cannot work here.")
            print("Run it from a network that allows port 25, or pass --skip-preflight to try anyway.")
            return 1

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    domains: dict[str, DomainInfo] = {}
    try:
        contacts = Table("business_contacts", metadata, autoload_with=engine)
        with engine.connect() as connection:
            queue = load_contacts(connection, contacts, args)
        if not queue:
            print("No contacts need email verification (use --recheck to verify again).")
            return 0

        if args.engine == "reacher":
            print(f"Verifying {len(queue)} contact(s) with Reacher. No email is sent.")
        else:
            print(f"Verifying {len(queue)} contact(s). Probing as {mail_from} (HELO {helo}). No email is sent.")
        final: dict[int, dict] = {}
        report_rows: dict[int, dict] = {}
        last_call: dict[str, float] = {}
        connection_failures = 0
        no_smtp_streak = 0
        warned_helo = False
        aborted = False

        def process(contact: dict) -> dict:
            nonlocal connection_failures, no_smtp_streak, warned_helo
            if args.engine == "reacher":
                values = verify_contact_reacher(contact, client, args, last_call)
                details = values["email_check_details"]
                summary = details.get("reacher") or {}
                provider, catch_all = "reacher", details.get("catch_all")
                if summary.get("can_connect_smtp") is False:
                    no_smtp_streak += 1
                elif summary.get("can_connect_smtp"):
                    no_smtp_streak = 0
                helo_used = (summary.get("smtp_error") or "")
                if not warned_helo and "fully-qualified" in helo_used.casefold():
                    warned_helo = True
                    print("  Warning: the mail server refused Reacher's HELO name. Restart the container with "
                          "-e RCH__HELLO_NAME=<a fully-qualified hostname> (see README).")
            else:
                domain = contact["email"].rpartition("@")[2].casefold()
                if domain not in domains:
                    domains[domain] = inspect_domain(domain, resolver, args, helo, mail_from)
                info = domains[domain]
                was_unreachable = info.unreachable
                values = verify_contact(contact, info, args)
                details = values["email_check_details"]
                provider, catch_all = info.provider, info.catch_all
                if info.unreachable and not was_unreachable:
                    connection_failures += 1
            chosen = values.get("email", contact["email"])
            tags = [provider] + (["catch-all"] if catch_all else []) + (["role-based"] if details["role_based"] else [])
            review = " NEEDS REVIEW" if details.get("needs_review", values["email_status"] in NEEDS_REVIEW_STATUSES) else ""
            print(f"  [{values['email_status']}]{review} {contact['name']} <{chosen}> ({', '.join(tags)}) {details.get('note', '')}".rstrip())
            if not args.dry_run:
                with engine.begin() as connection:
                    connection.execute(update(contacts).where(contacts.c.id == contact["id"]).values(**values))
            report_rows[contact["id"]] = {
                "contact_id": contact["id"], "business_id": contact["business_id"], "name": contact["name"],
                "original_email": contact["email"], "email": chosen, "status": values["email_status"],
                "provider": provider, "catch_all": catch_all, "role_based": details["role_based"],
                "probes": len(details["probes"]), "note": details.get("note", ""),
                "last_reply": (details["probes"][-1]["message"] if details["probes"] else ""),
            }
            return values

        pending = list(queue)
        for round_number in range(args.retry_rounds + 1):
            retry: list[dict] = []
            if round_number:
                print(f"Retry pass {round_number}: waiting {args.retry_wait:.0f}s for greylisting to expire...")
                time.sleep(args.retry_wait)
                for info in domains.values():
                    info.unreachable = False
                no_smtp_streak = 0
            for contact in pending:
                try:
                    values = process(contact)
                except ReacherUnavailable as error:
                    print(f"{error} Stopping; rerun once Reacher is back.")
                    aborted = True
                    break
                final[contact["id"]] = values
                if was_greylisted(values):
                    retry.append(contact)
                if connection_failures >= MAX_CONNECTION_FAILURES_BEFORE_ABORT and not any(
                    d.catch_all_tested and not d.unreachable for d in domains.values()
                ):
                    print("Mail servers are unreachable on port 25; your network or ISP is probably blocking it. Stopping.")
                    aborted = True
                    break
                if no_smtp_streak >= MAX_CONSECUTIVE_NO_SMTP:
                    print(f"Reacher could not open an SMTP connection to {no_smtp_streak} servers in a row; outbound "
                          "port 25 is probably blocked for the container or your network. Stopping.")
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
        for info in domains.values():
            if info.session is not None:
                info.session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
