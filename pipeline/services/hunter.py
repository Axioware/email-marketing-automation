"""Hunter.io (https://hunter.io/api-documentation/v2): the other way to find decision makers and verify their emails.

Chosen on the Settings page ("Find and verify contacts with": Hunter). Then:
- Find decision makers (stakeholders.py) asks Hunter's Domain Search for the people with an address at the business's
  domain (or, without a website, at the company name) and stores them as contacts, the best role first.
- Verify emails (verification.py) checks each contact's address with Hunter's Email Verifier and stores the prospect
  the same way Reacher's results are stored.

Every call uses Hunter credits (a domain search: 1 per 10 emails returned; a verification: 1), so each search asks for
a few emails only and verification checks only the contact's own address unless told otherwise. The key is
HUNTER_API_KEY in .env, sent as the X-API-KEY header.
"""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://api.hunter.io/v2"
TIMEOUT = 40  # seconds; Hunter answers a verification within about 20 s or says it is still working (202)
VERIFY_RETRIES = 3  # a 202 (verification still running) is asked again this many times
VERIFY_RETRY_WAIT = 5.0
PROVIDER = "hunter"

# Hunter's verification status -> (our status, our verdict)
STATUS = {
    "valid": ("deliverable", "deliverable"),
    "invalid": ("undeliverable", "mailbox_not_found"),
    "accept_all": ("risky", "catch_all"),
    "webmail": ("unknown", "webmail"),  # a free webmail address (gmail, outlook...): Hunter cannot verify it
    "disposable": ("undeliverable", "disposable"),
    "unknown": ("unknown", "unknown"),
}
NOTES = {
    "deliverable": None,
    "mailbox_not_found": "Hunter: the mail server says this mailbox does not exist",
    "invalid_syntax": "Hunter: not a valid email address",
    "no_mail_server": "Hunter: the domain has no mail server",
    "catch_all": "Hunter: the domain accepts mail for any address (catch-all); existence cannot be proven",
    "webmail": "Hunter: a webmail address (e.g. gmail.com); Hunter cannot verify these",
    "disposable": "Hunter: a disposable email address",
    "blocked": "Hunter: the mail server blocked the verification",
    "unknown": "Hunter could not verify this address",
    "check_failed": "Hunter did not finish the verification; try again later",
}


class HunterError(RuntimeError):
    """One call failed (the business or address is skipped and can be retried)."""


class HunterStopped(HunterError):
    """Hunter cannot be used right now (bad key, no credits, rate limit): the run stops; results so far are kept."""


def api_key() -> str:
    return os.environ.get("HUNTER_API_KEY", "").strip()


def configured() -> bool:
    return bool(api_key())


def base_url() -> str:
    return (os.environ.get("HUNTER_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")


def _error_detail(error: HTTPError) -> str:
    try:
        body = json.loads(error.read().decode("utf-8") or "{}")
        problems = body.get("errors") or []
        return "; ".join(str(item.get("details") or item.get("id") or item) for item in problems) or str(body)[:200]
    except (ValueError, OSError, AttributeError):
        return ""


def _get(path: str, params: dict) -> tuple[int, dict]:
    if not configured():
        raise HunterStopped("HUNTER_API_KEY is not set in .env")
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    request = Request(f"{base_url()}/{path}?{query}",
                      headers={"Accept": "application/json", "X-API-KEY": api_key()})
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - Hunter's API, from our settings
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as error:
        detail = _error_detail(error)
        if error.code == 401:
            raise HunterStopped(f"Hunter refused the key (HUNTER_API_KEY): {detail}".rstrip(": ")) from error
        if error.code in (403, 429):
            raise HunterStopped(f"Hunter's limit is reached (no credits left or too many requests): {detail}"
                                .rstrip(": ")) from error
        raise HunterError(f"Hunter answered HTTP {error.code}: {detail}".rstrip(": ")) from error
    except (URLError, OSError, TimeoutError) as error:
        raise HunterError(f"Hunter could not be reached ({getattr(error, 'reason', error)})") from error
    except ValueError as error:
        raise HunterError("Hunter answered something that is not JSON") from error


def domain_search(domain: str | None = None, company: str | None = None, limit: int = 10) -> dict:
    """The people Hunter knows at a domain (or company name): {"domain", "organization", "pattern", "emails": [...]};
    each email has value, type (personal/generic), confidence, first_name, last_name, position, seniority,
    department, linkedin, sources and verification."""
    if not domain and not company:
        raise HunterError("no domain or company name to search for")
    _, body = _get("domain-search", {"domain": domain, "company": None if domain else company, "limit": limit})
    return body.get("data") or {}


def verify(address: str, retries: int = VERIFY_RETRIES, wait: float = VERIFY_RETRY_WAIT) -> dict:
    """Hunter's verification of one address (its `data`). A verification Hunter is still running (HTTP 202) is asked
    again a few times; if it never finishes, {"status": None}."""
    for attempt in range(retries + 1):
        code, body = _get("email-verifier", {"email": address})
        if code != 202:
            return body.get("data") or {}
        if attempt < retries:
            time.sleep(wait)
    return {"status": None}


def outcome(address: str, data: dict) -> dict:
    """A verification as the outcome verification.py stores: status, verdict, reason, note, summary."""
    status_name = data.get("status")
    if status_name is None:
        status, reason = "unknown", "check_failed"
    else:
        status, reason = STATUS.get(status_name, ("unknown", "unknown"))
    if status_name == "invalid":
        if data.get("regexp") is False:
            reason = "invalid_syntax"
        elif data.get("mx_records") is False:
            reason = "no_mail_server"
    if status_name == "unknown" and data.get("block"):
        reason = "blocked"
    summary = {key: data.get(key) for key in ("status", "result", "score", "regexp", "gibberish", "disposable",
                                              "webmail", "mx_records", "smtp_server", "smtp_check", "accept_all",
                                              "block")}
    summary["is_catch_all"] = bool(data.get("accept_all")) or status_name == "accept_all"
    return {"address": address, "status": status, "verdict": status_name or "unknown", "reason": reason,
            "note": NOTES.get(reason), "summary": summary, "retryable": status == "unknown", "provider": PROVIDER}
