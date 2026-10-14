"""GoHighLevel (LeadConnector API v2) client: contacts and the custom fields the sending workflow reads.

Settings (.env): GHL_API_KEY (a Private Integration token of the sub-account, scopes contacts and locations
custom fields), GHL_LOCATION_ID (the sub-account id). Custom fields are found by their keys (FIELD_KEYS), so
their ids never need to be configured. Requests carry their own User-Agent: Cloudflare in front of the API refuses
Python's default one.
"""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
USER_AGENT = "email-marketing-automation/1.0"
TIMEOUT = 30
RETRIES = 3  # on 429 (rate limit: 100 requests per 10 s) and 5xx answers

# The contact custom fields the GHL workflows use, by their GHL keys.
FIELD_KEYS = {
    "subject": "contact.email_subject",
    "body": "contact.email_body",
    "due": "contact.email_due_at",
    "count": "contact.followup_count",
    "type": "contact.initial_email_type",
}
OUTREACH_TAG = "outreach-app"  # every contact this app creates; the new-lead workflow filters on it

_field_cache: dict[str, dict[str, str]] = {}


class GhlError(RuntimeError):
    """GoHighLevel refused or could not be reached; the message says which and why."""


def settings() -> tuple[str, str]:
    return os.environ.get("GHL_API_KEY", "").strip(), os.environ.get("GHL_LOCATION_ID", "").strip()


def configured() -> bool:
    return all(settings())


class Client:
    def __init__(self, token: str | None = None, location_id: str | None = None, base_url: str | None = None):
        env_token, env_location = settings()
        self.token = token or env_token
        self.location_id = location_id or env_location
        self.base_url = (base_url or os.environ.get("GHL_BASE_URL", "").strip() or BASE_URL).rstrip("/")
        if not self.token or not self.location_id:
            raise GhlError("GoHighLevel is not set up: add GHL_API_KEY and GHL_LOCATION_ID to .env.")
        from django.conf import settings as django_settings

        if getattr(django_settings, "TESTING", False) and self.base_url == BASE_URL:
            raise GhlError("Tests never call the real GoHighLevel; set GHL_BASE_URL to a fake server.")

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, RETRIES + 1):
            request = Request(f"{self.base_url}{path}", data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Version": API_VERSION, "Accept": "application/json",
                "Content-Type": "application/json", "User-Agent": USER_AGENT})
            try:
                with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - fixed https API host
                    return json.loads(response.read() or b"{}")
            except HTTPError as error:
                detail = _error_detail(error)
                if (error.code == 429 or error.code >= 500) and attempt < RETRIES:
                    time.sleep(2 * attempt)
                    continue
                raise GhlError(f"GoHighLevel answered HTTP {error.code} to {method} {path.split('?')[0]}: {detail}") \
                    from error
            except (URLError, OSError, ValueError) as error:
                if attempt < RETRIES:
                    time.sleep(2 * attempt)
                    continue
                raise GhlError(f"GoHighLevel could not be reached ({getattr(error, 'reason', error)})") from error
        raise GhlError("GoHighLevel did not answer")  # not reached

    def field_ids(self) -> dict[str, str]:
        """{"subject": id, "body": id, ...} for FIELD_KEYS; raises GhlError naming any field that is missing."""
        if self.location_id not in _field_cache:
            data = self.request("GET", f"/locations/{self.location_id}/customFields?model=contact")
            by_key = {field.get("fieldKey"): field.get("id") for field in data.get("customFields", [])}
            missing = [key for key in FIELD_KEYS.values() if key not in by_key]
            if missing:
                raise GhlError("GoHighLevel is missing contact custom fields: " + ", ".join(missing)
                               + " (create them in Settings > Custom Fields; see the GHL setup guide).")
            _field_cache[self.location_id] = {name: by_key[key] for name, key in FIELD_KEYS.items()}
        return _field_cache[self.location_id]

    def upsert_contact(self, contact: dict) -> tuple[str, bool]:
        """Create or update a contact (matched by email). Returns (contact id, whether it was new)."""
        data = self.request("POST", "/contacts/upsert", {"locationId": self.location_id, **contact})
        contact_id = (data.get("contact") or {}).get("id")
        if not contact_id:
            raise GhlError("GoHighLevel did not return the contact id")
        return contact_id, bool(data.get("new"))

    def set_fields(self, contact_id: str, values: dict) -> None:
        """Write custom fields by name (FIELD_KEYS); None or "" clears a field."""
        ids = self.field_ids()
        fields = [{"id": ids[name], "field_value": "" if value is None else value} for name, value in values.items()]
        self.request("PUT", f"/contacts/{contact_id}", {"customFields": fields})

    def get_fields(self, contact_id: str) -> dict:
        """The contact's custom fields by name (absent = empty)."""
        ids = self.field_ids()
        names = {field_id: name for name, field_id in ids.items()}
        contact = self.request("GET", f"/contacts/{contact_id}").get("contact") or {}
        return {names[field["id"]]: field.get("value") for field in contact.get("customFields", []) if field.get("id") in names}


def _error_detail(error: HTTPError) -> str:
    try:
        body = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return error.reason or ""
    message = body.get("message") if isinstance(body, dict) else None
    if isinstance(message, list):
        message = "; ".join(map(str, message))
    return str(message or body)[:300]
