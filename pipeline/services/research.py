"""Module 2: an LLM agent reads each business's website (at most five pages) and scores it for outreach.

Pages are read over plain HTTP plus Jina Reader (which renders them in its own browser), so no browser runs here and
the step runs on the web app (Vercel) as well.

The model's instructions are the campaign prompt of the business's campaign, exactly as stored (campaign_prompts
table, edited in the admin); nothing is added to it. The app only fixes the shape of each answer: AgentResponse (plus
the campaign prompt's output fields, filled when scoring and saved as the profile's `extracted`), sent
as a strict JSON schema whose field descriptions say how to read another page, score, or stop. A business whose
campaign has no campaign prompt is not researched."""
import ipaddress
import json
import math
import os
import re
import socket
import time
from collections import deque
from datetime import timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import CommandError
from django.db.models import F, Q
from django.utils import timezone
from openai import OpenAI

from bs4 import BeautifulSoup

try:  # better text extraction when Jina Reader is not used (requirements-worker.txt); plain page text otherwise
    import trafilatura
except ImportError:
    trafilatura = None

from pipeline.management.base import PARTIAL, OutOfTime, call_timeout, out_of_time, time_limit_note
from pipeline.models import Business, BusinessWebsiteProfile, check_output_fields
from pipeline.services.llm import complete, make_llm_client, rejected_output, tune
from pipeline.services.dbthread import outside_event_loop

MAX_PAGES_PER_BUSINESS = 5
MAX_AGENT_RETRIES = 3
MAX_CONTENT_CHARS = 16000
MAX_RESEARCH_LINKS = 200
MAX_PAGE_LINKS = 100
# What one agent request may contain. Free LLM tiers limit tokens per minute (Groq: 8,000), so the request carries
# only what the agent needs: a capped page text and the unvisited links it can choose from.
MAX_PROMPT_CONTENT_CHARS = 6000
MAX_PROMPT_LINKS = 50
MAX_COMPLETION_TOKENS = 2500  # the decision is short; Groq counts this budget against the per-minute limit
MAX_PROMPT_LINK_TEXT = 80
PRIVATE_CONTEXT_KEYS = {"visited_identities", "current_page_links", "previously_discovered_links"}  # validation only
JINA_REQUESTS_PER_MINUTE = 20  # Jina Reader's free limit without a key; with JINA_API_KEY it allows more
JINA_KEYED_REQUESTS_PER_MINUTE = 200
JINA_RATE_WINDOW_SECONDS = 60
JINA_TIMEOUT = 30
PAGE_TIMEOUT = 15
MAX_HTML_BYTES = 3_000_000
LLM_TIMEOUT = 60
# With a deadline (the web app on Vercel): a business starts only with this much time left, and the agent is asked
# to score from what it read once less than PAGE_RESERVE_SECONDS are left (another page plus a decision).
BUSINESS_RESERVE_SECONDS = 150
PAGE_RESERVE_SECONDS = 100
TIME_LIMIT_NOTE = "the run's time limit was reached"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
class AgentResponse(BaseModel):
    """One step of the research: read another page of the site, give the score, or stop. Sent to the model as the
    answer's JSON schema (with these descriptions); validate_agent_response checks it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["SCRAPE", "SCORE", "EXIT"] = Field(
        ..., description="SCRAPE: read one more page of this website (put it in url). SCORE: you have enough evidence "
                         "to score the business. EXIT: the website cannot be used (nothing useful, blocked, parked). "
                         "When the input has must_score true, answer SCORE.")
    url: str | None = Field(..., description="SCRAPE only: one unvisited URL copied exactly from available_links in "
                                             "the input; null otherwise.")
    score: int | None = Field(..., ge=0, le=100, description="SCORE only: the qualification score, 0-100; null "
                                                            "otherwise.")
    reasons: list[str] | None = Field(..., description="SCORE only: short reasons for the score, each based on what "
                                                       "the website shows; null otherwise.")
    reasoning: str | None = Field(..., description="SCORE only: how the score was reached (for audits, not outreach "
                                                   "copy); when must_score is true, say the information was incomplete "
                                                   "and why (must_score_reason); null otherwise.")
    reason: str | None = Field(..., description="SCRAPE: why that page is worth reading. EXIT: why the website cannot "
                                                "be used. Null for SCORE.")
    summary: str = Field(..., min_length=1, description="What the current page says about the business, in a "
                                                        "sentence or two.")
    relevant_findings: list[str] = Field(..., description="Facts from the current page that matter for the score "
                                                          "or for writing to the business; empty when none.")


FIELD_PYTHON_TYPES = {"text": str, "integer": int, "number": float, "yes/no": bool, "text list": list[str],
                      "integer list": list[int]}
_response_models: dict = {}


def response_model(output_fields: list[dict] | None = None) -> type[BaseModel]:
    """AgentResponse plus the campaign prompt's output fields (filled when scoring, null otherwise)."""
    fields = check_output_fields(output_fields or [])
    key = json.dumps(fields, sort_keys=True)
    if key not in _response_models:
        extra = {
            field["name"]: (FIELD_PYTHON_TYPES[field["type"]] | None,
                            Field(..., description=f"SCORE only: {field['description']} Null when not stated, and for "
                                                   "SCRAPE/EXIT."))
            for field in fields
        }
        _response_models[key] = create_model("AgentResponse", __base__=AgentResponse, **extra) if extra else AgentResponse
    return _response_models[key]


SCHEMA_TYPES = {  # an output field's JSON schema (without null) -> its type name
    json.dumps({"type": "string"}): "text",
    json.dumps({"type": "integer"}): "integer",
    json.dumps({"type": "number"}): "number",
    json.dumps({"type": "boolean"}): "yes/no",
    json.dumps({"items": {"type": "string"}, "type": "array"}): "text list",
    json.dumps({"items": {"type": "integer"}, "type": "array"}): "integer list",
}
LABELS = ("title", "description")


class SchemaError(ValueError):
    """The edited answer schema cannot be used; the message says what to fix."""


def _without_labels(definition: dict) -> dict:
    return {key: value for key, value in definition.items() if key not in LABELS}


def _field_type(name: str, definition) -> str:
    """The type name of an output field's schema: plain, nullable through anyOf, or "type": [x, "null"]."""
    if not isinstance(definition, dict):
        raise SchemaError(f'"{name}" must be an object such as {{"type": "string", "description": "..."}}.')
    core = _without_labels(definition)
    if "anyOf" in core and len(core) == 1 and isinstance(core["anyOf"], list):
        options = [option for option in core["anyOf"] if option != {"type": "null"}]
        core = options[0] if len(options) == 1 and isinstance(options[0], dict) else None
    elif isinstance(core.get("type"), list):
        kinds = [kind for kind in core["type"] if kind != "null"]
        core = {**core, "type": kinds[0]} if len(kinds) == 1 else None
    kind = SCHEMA_TYPES.get(json.dumps(_without_labels(core), sort_keys=True)) if isinstance(core, dict) else None
    if kind is None:
        raise SchemaError(f'"{name}" has a type the app cannot store; use string, integer, number, boolean, or an '
                          'array of strings or integers (e.g. {"type": "array", "items": {"type": "string"}}).')
    return kind


def check_answer_schema(value) -> tuple[dict, list[dict]]:
    """(the schema as the AI receives it, the output fields in it) for an edited answer schema, a JSON text or object.
    The app's own fields must keep their types (only their descriptions may change); any other property is an output
    field: a text, integer, number, yes/no, text list or integer list that may be null."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise SchemaError(f"This is not valid JSON: {error}.") from error
    if not isinstance(value, dict) or not isinstance(value.get("properties"), dict):
        raise SchemaError('The schema must be a JSON object with "properties".')
    app = AgentResponse.model_json_schema()
    properties = value["properties"]
    missing = [name for name in app["properties"] if name not in properties]
    if missing:
        raise SchemaError(f"The app's own fields must stay: {', '.join(missing)} missing. Only their descriptions "
                          "may change.")
    schema_properties, output_fields = {}, []
    for name, definition in properties.items():
        if name in app["properties"]:
            if not isinstance(definition, dict) or _without_labels(definition) != _without_labels(app["properties"][name]):
                raise SchemaError(f'"{name}" is one of the app\'s own fields: only its "description" may change.')
            description = str(definition.get("description") or app["properties"][name].get("description") or "")
            schema_properties[name] = {**app["properties"][name], "description": description}
            continue
        kind = _field_type(name, definition)
        description = " ".join(str(definition.get("description") or "").split())
        output_fields.append({"name": name, "type": kind, "description": description})
    try:
        output_fields = check_output_fields(output_fields)
    except DjangoValidationError as error:
        raise SchemaError(" ".join(error.messages).replace("Field ", "Property ")) from error
    for field in output_fields:
        definition = properties[field["name"]]
        plain = response_model([field]).model_json_schema()["properties"][field["name"]]
        schema_properties[field["name"]] = {**plain, "title": definition.get("title") or plain["title"],
                                            "description": field["description"]}
    schema = {"additionalProperties": False, "description": str(value.get("description") or app.get("description")),
              "properties": schema_properties, "required": list(schema_properties),
              "title": str(value.get("title") or "AgentResponse"), "type": "object"}
    return schema, output_fields


def schema_for(answer_schema: dict | None, output_fields: list[dict] | None) -> dict:
    """The JSON schema the AI answers in: the prompt's edited one, or the app's fields plus the output fields."""
    return answer_schema or response_model(output_fields).model_json_schema()


def answer_format(output_fields: list[dict] | None = None, answer_schema: dict | None = None) -> str:
    """The answer schema as the model receives it, for showing (and editing) on a campaign prompt's page."""
    return json.dumps(schema_for(answer_schema, output_fields), indent=2)


def clean_value(value, kind: str):
    """An output field's value as stored: text trimmed, lists without blanks or repeats (case-insensitive);
    None when empty."""
    if value is None:
        return None
    if kind == "text":
        return " ".join(value.split())[:1000] or None
    if kind in ("text list", "integer list"):
        cleaned, seen = [], set()
        for item in value:
            item = " ".join(item.split())[:200] if isinstance(item, str) else item
            marker = item.casefold() if isinstance(item, str) else item
            if item not in ("", None) and marker not in seen:
                seen.add(marker)
                cleaned.append(item)
        return cleaned[:50] or None
    if kind == "integer" and value < 0:
        return None
    return value


def extracted_values(decision: BaseModel, output_fields: list[dict]) -> dict:
    return {field["name"]: clean_value(getattr(decision, field["name"], None), field["type"])
            for field in check_output_fields(output_fields or [])}


class ResearchError(RuntimeError):
    pass


def json_default(value):
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.request_times = deque()

    def acquire(self, max_wait: float | None = None) -> None:
        """Wait for a free slot; with `max_wait`, give up (ResearchError) instead of waiting longer than that."""
        while True:
            now = time.monotonic()
            while self.request_times and now - self.request_times[0] >= self.window_seconds:
                self.request_times.popleft()
            if len(self.request_times) < self.max_requests:
                self.request_times.append(now)
                return
            wait_seconds = self.window_seconds - (now - self.request_times[0])
            if max_wait is not None and wait_seconds > max_wait:
                raise ResearchError("Jina Reader's per-minute limit is used up")
            time.sleep(max(0, wait_seconds))


class JinaReader:
    """Jina Reader (r.jina.ai) opens a page in its own browser and returns the rendered text, the final URL and the
    page's links, so no browser runs here. JINA_API_KEY (optional) raises its rate limit."""

    def __init__(self, max_wait: float | None = None):
        self.key = os.environ.get("JINA_API_KEY", "").strip()
        self.rate_limiter = SlidingWindowRateLimiter(
            JINA_KEYED_REQUESTS_PER_MINUTE if self.key else JINA_REQUESTS_PER_MINUTE,
            JINA_RATE_WINDOW_SECONDS,
        )
        self.max_wait = max_wait  # with a deadline: never wait out the rate limit past it

    def read(self, url: str, timeout: float = JINA_TIMEOUT) -> dict:
        """{url (after redirects), title, content (markdown), links [(text, url)], status (the page's HTTP status)}."""
        self.rate_limiter.acquire(self.max_wait)
        headers = {"Accept": "application/json", "X-With-Links-Summary": "true", "User-Agent": "outreach-app"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            with urlopen(Request(f"https://r.jina.ai/{url}", headers=headers), timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise ResearchError(f"Jina Reader request failed ({type(error).__name__}).") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        content = ((data or {}).get("content") or "").strip()
        if not content:
            raise ResearchError("Jina Reader returned no readable content.")
        links = data.get("links") or {}
        pairs = (list(links.items()) if isinstance(links, dict) else
                 [(item.get("text") or "", item.get("url") or "") for item in links if isinstance(item, dict)])
        return {"url": data.get("url") or url, "title": data.get("title") or "", "content": content[:MAX_CONTENT_CHARS],
                "links": [(str(text or ""), str(href)) for text, href in pairs if href],
                "status": data.get("httpStatus") if isinstance(data.get("httpStatus"), int) else None}


class PublicRedirects(HTTPRedirectHandler):
    """Follow a page's redirects only to public addresses (a website must not point us at this machine)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_http_url(newurl):
            raise ResearchError("Page redirected to a non-public address.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url: str, timeout: float = PAGE_TIMEOUT) -> tuple[str, str]:
    """(final URL, HTML) of a public web page."""
    try:
        with build_opener(PublicRedirects).open(Request(url, headers=BROWSER_HEADERS), timeout=timeout) as response:
            if "html" not in (response.headers.get("Content-Type") or "html").casefold():
                raise ResearchError("The page is not HTML.")
            raw = response.read(MAX_HTML_BYTES)
            return response.geturl(), raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except HTTPError as error:
        raise ResearchError(f"Page returned HTTP {error.code}.") from error
    except (URLError, OSError, ValueError) as error:
        raise ResearchError(f"Page could not be loaded ({type(error).__name__}).") from error


def normalize_url(value: str, base_url: str | None = None) -> str | None:
    candidate = urljoin(base_url, value.strip()) if base_url else value.strip()
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.casefold()
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").casefold().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        return None

    hostname = hostname.removeprefix("www.")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        netloc = f"{netloc}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in TRACKING_QUERY_KEYS
            and not key.casefold().startswith("utm_")
        )
    )
    return urlunsplit((scheme, netloc, path or "/", query, ""))


def url_identity(value: str) -> tuple[str, str, str] | None:
    normalized = normalize_url(value)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    port = parsed.port
    effective_port = port if port not in (80, 443) else None
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    authority = f"{host}:{effective_port}" if effective_port else host
    return authority, parsed.path or "/", parsed.query


def is_public_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not hostname:
            return False
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            return False
        try:
            addresses = {ipaddress.ip_address(hostname)}
        except ValueError:
            addresses = {ipaddress.ip_address(result[4][0]) for result in lookup_host(hostname, parsed.port or 80)}
        return bool(addresses) and all(address.is_global for address in addresses)
    except (OSError, ValueError):
        return False


DNS_ATTEMPTS = 3  # a lookup the network's DNS fails "temporarily" is asked again, not taken as "no such site"


def lookup_host(hostname: str, port: int) -> list:
    """socket.getaddrinfo, retried when the DNS server fails temporarily (EAI_AGAIN)."""
    for attempt in range(DNS_ATTEMPTS):
        try:
            return socket.getaddrinfo(hostname, port)
        except socket.gaierror as error:
            if error.errno != socket.EAI_AGAIN or attempt == DNS_ATTEMPTS - 1:
                raise
            time.sleep(1 + attempt)
    return []


def is_same_site(url: str, homepage_url: str) -> bool:
    target_host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    home_host = (urlsplit(homepage_url).hostname or "").casefold().removeprefix("www.")
    return bool(home_host and (target_host == home_host or target_host.endswith(f".{home_host}")))


def extract_page(requested_url: str, homepage_url: str, jina_reader: JinaReader | None,
                 deadline: float | None = None) -> dict:
    """Read one page of the business's website: its HTML (links, emails) over plain HTTP, and its rendered text and
    links through Jina Reader (pages built by JavaScript included). Either one is enough."""
    if not is_public_http_url(requested_url):
        raise ResearchError("Refusing to fetch a non-public or unsupported URL.")
    html = current_url = rendered = fetch_error = None
    try:
        final_url, html = fetch_html(requested_url, call_timeout(deadline, PAGE_TIMEOUT))
        current_url = normalize_url(final_url)
    except ResearchError as error:
        fetch_error = error
    if jina_reader:
        try:
            rendered = jina_reader.read(current_url or requested_url, call_timeout(deadline, JINA_TIMEOUT))
        except ResearchError as error:
            print(f"Jina extraction unavailable; using the page's HTML ({error}).")
    if rendered and rendered["status"] and rendered["status"] >= 400:
        raise ResearchError(f"Page returned HTTP {rendered['status']}.")
    if html is None and rendered is None:
        raise fetch_error or ResearchError("The page could not be read.")
    current_url = current_url or normalize_url(rendered["url"]) or requested_url
    if not current_url or not is_same_site(current_url, homepage_url) or not is_public_http_url(current_url):
        raise ResearchError("Page redirected outside the business website.")

    emails = set()
    links = []
    seen_links = set()

    def add_link(href: str, text: str) -> None:
        href = (href or "").strip()
        if href.casefold().startswith("mailto:"):
            emails.update(address.casefold() for address in EMAIL_PATTERN.findall(href[7:]))
            return
        link_url = normalize_url(href, current_url)
        identity = url_identity(link_url) if link_url else None
        if not link_url or not identity or identity in seen_links:
            return
        seen_links.add(identity)
        links.append({"url": link_url, "text": " ".join((text or "").split()),
                      "same_site": is_same_site(link_url, homepage_url)})

    soup = BeautifulSoup(html, "html.parser") if html else None
    if soup is not None:
        emails.update(address.casefold() for address in EMAIL_PATTERN.findall(soup.get_text(" ", strip=True)))
        for anchor in soup.select("a[href]"):
            add_link(anchor.get("href", ""), anchor.get_text(" ", strip=True))
    for text, href in (rendered or {}).get("links", []):
        add_link(href, text)

    content = rendered["content"] if rendered else None
    if not content and soup is not None:
        for element in soup.select(
            "script, style, noscript, svg, nav, header, footer, aside, form, "
            "[role='navigation'], [role='banner'], [role='contentinfo']"
        ):
            element.decompose()
        if trafilatura is not None:
            content = trafilatura.extract(str(soup), include_comments=False, include_tables=True, include_links=False,
                                          output_format="txt")
        content = content or soup.get_text("\n", strip=True)
    content = "\n".join(line.strip() for line in (content or "").splitlines() if line.strip())
    content = content[:MAX_CONTENT_CHARS]
    emails.update(address.casefold() for address in EMAIL_PATTERN.findall(content))
    return {
        "url": current_url,
        "content": content,
        "links": links[:MAX_PAGE_LINKS],
        "emails": sorted(emails),
    }


def prompt_context(context: dict) -> dict:
    """The part of the research context sent to the model: no internal bookkeeping, no duplicate link lists."""
    payload = {key: value for key, value in context.items() if key not in PRIVATE_CONTEXT_KEYS}
    payload["cleaned_page_content"] = (context.get("cleaned_page_content") or "")[:MAX_PROMPT_CONTENT_CHARS]
    payload["available_links"] = [
        {"url": link["url"], "text": (link.get("text") or "")[:MAX_PROMPT_LINK_TEXT]}
        for link in context.get("available_links", [])[:MAX_PROMPT_LINKS]
    ]
    return payload


NULLABLE_FIELDS = ("url", "score", "reasons", "reasoning", "reason")
PAGE_LIMIT_NOTE = "the five-page limit was reached"


def parse_decision(content: str, output_fields: list[dict] | None = None) -> BaseModel:
    """The agent's answer; fields it left out that may be null count as null (models sometimes drop them)."""
    model = response_model(output_fields)
    data = json.loads(content or "")
    if isinstance(data, dict):
        for name in NULLABLE_FIELDS:
            data.setdefault(name, None)
        for name in model.model_fields:
            if name not in AgentResponse.model_fields:
                data.setdefault(name, None)
    return model.model_validate(data)


def validate_agent_response(client: OpenAI, model: str, context: dict, must_score: bool, system_prompt: str,
                            score_note: str = PAGE_LIMIT_NOTE, deadline: float | None = None,
                            output_fields: list[dict] | None = None, answer_schema: dict | None = None) -> dict:
    """One agent decision. With must_score the agent has to SCORE now; score_note says why (shown to the agent and
    added to its reasoning)."""
    feedback = None
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "business_research_action",
            "strict": True,
            "schema": schema_for(answer_schema, output_fields),
        },
    }
    for _ in range(MAX_AGENT_RETRIES):
        request_data = {**prompt_context(context), "must_score": must_score, "validation_feedback": feedback}
        if must_score:
            request_data["must_score_reason"] = score_note
        user_message = json.dumps(request_data, ensure_ascii=False, default=json_default, allow_nan=False)
        try:
            response = complete(
                client,
                deadline,
                timeout=LLM_TIMEOUT,
                model=model,
                **tune(model, temperature=0.2, max_completion_tokens=MAX_COMPLETION_TOKENS),
                response_format=response_format,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            content = response.choices[0].message.content or ""
        except Exception as error:
            content = rejected_output(error)  # the provider refused the answer's shape: check it here instead
            if content is None:
                raise
        try:
            decision = parse_decision(content, output_fields)
        except (ValueError, ValidationError):
            feedback = ("Return a response matching the required strict action schema, with every field present "
                        "(unused ones as null).")
            continue

        action = decision.action
        page_notes = {
            "summary": decision.summary.strip()[:2000],
            "relevant_findings": [
                item.strip()[:500] for item in decision.relevant_findings if item.strip()
            ][:12],
        }
        if action == "SCORE":
            if (
                decision.score is not None
                and decision.reasons is not None
                and decision.reasoning is not None
                and decision.url is None
                and decision.reason is None
            ):
                reasoning = decision.reasoning.strip()
                if must_score:
                    reasoning = (
                        f"{reasoning}\n\n"
                        f"The score is based on incomplete information because {score_note}."
                    ).strip()
                return {
                    "action": action,
                    **page_notes,
                    "qualification_score": decision.score,
                    "qualification_reasons": decision.reasons,
                    "agent_reasoning": reasoning,
                    "extracted": extracted_values(decision, output_fields),
                }
            feedback = "SCORE requires score, reasons, and reasoning; set url and reason to null."
            continue

        if action == "EXIT" and not must_score:
            if decision.reason is None or not decision.reason.strip() or any(
                value is not None
                for value in (decision.url, decision.score, decision.reasons, decision.reasoning)
            ):
                feedback = "EXIT requires reason and null URL/scoring fields."
                continue
            return {
                "action": action,
                **page_notes,
                "qualification_score": None,
                "qualification_reasons": [],
                "agent_reasoning": decision.reason,
                "extracted": None,
            }

        if action == "SCRAPE" and not must_score:
            selected = normalize_url(decision.url, context["current_page_url"]) if decision.url else None
            selected_identity = url_identity(selected) if selected else None
            if not selected_identity:
                feedback = "SCRAPE requires a valid URL from the current website."
                continue
            if selected_identity and selected_identity in context["visited_identities"]:
                feedback = "This URL has already been scraped. Choose another URL or produce SCORE/EXIT."
                continue
            available_identities = {
                url_identity(link["url"])
                for link in context["available_links"][:MAX_PROMPT_LINKS]
                if link["same_site"]
            }
            if (
                selected_identity
                and selected_identity in available_identities
                and selected_identity not in context["visited_identities"]
            ):
                request_reason = (
                    decision.reason or decision.reasoning or
                    "The agent selected this page as potentially useful for qualification."
                )
                return {
                    "action": action,
                    "url": selected,
                    "reason": request_reason.strip()[:2000],
                    **page_notes,
                }
            feedback = "Choose an unvisited same-site URL from available_links or return SCORE/EXIT."
            continue

        feedback = (
            "The five-page limit is reached; return SCORE now."
            if must_score
            else "Choose SCORE, SCRAPE, or EXIT."
        )

    raise ResearchError(feedback or "Agent failed to return a valid decision.")


def research_business(
    client: OpenAI,
    model: str,
    business: dict,
    jina_reader: JinaReader | None,
    on_page=None,
    deadline: float | None = None,
    saved_pages: list[dict] | None = None,
) -> dict:
    """Research one business. `saved_pages`: the pages an interrupted run read (with the agent's decision on each, if
    it made one); the research continues from there instead of starting over."""
    homepage = normalize_url(business.get("website_url") or "")
    if not homepage or not is_public_http_url(homepage):
        raise ResearchError("Business website is missing, invalid, or not publicly accessible.")

    pages = []
    emails = set()
    discovered_links = {}
    visited_urls = set()
    visited_identities = set()
    next_url = homepage
    decide_only = False  # the last saved page still needs the agent's decision

    def remember(page: dict) -> None:
        visited_identities.add(url_identity(page["url"]))
        visited_urls.add(page["url"])
        emails.update(page.get("emails") or [])
        for link in page.get("links") or []:
            identity = url_identity(link["url"])
            if link.get("same_site") and identity and identity not in discovered_links:
                if len(discovered_links) < MAX_RESEARCH_LINKS:
                    discovered_links[identity] = link

    for page in saved_pages or []:
        if page.get("url") and url_identity(page["url"]):
            pages.append({"summary": "", "relevant_findings": [], "agent_action": None, "requested_next_url": None,
                          "request_reason": None, "links": [], "emails": [], "content": "", **page})
            remember(page)
    if pages:
        last = pages[-1]
        if last["agent_action"] == "SCRAPE" and last["requested_next_url"]:
            next_url = last["requested_next_url"]
        elif last["agent_action"] is None:
            decide_only = True
    system_prompt = business["qualification_prompt"].strip()  # the campaign prompt, exactly as stored
    output_fields = business.get("output_fields") or []
    answer_schema = business.get("answer_schema")
    business = {key: value for key, value in business.items()
                if key not in ("qualification_prompt", "output_fields", "answer_schema")}  # in the prompt and format

    while len(pages) < MAX_PAGES_PER_BUSINESS or decide_only:
        if decide_only:  # an interrupted run read this page but the agent had not decided on it yet
            decide_only = False
            page_record = pages[-1]
            result = page_record
        else:
            requested = normalize_url(next_url)
            requested_identity = url_identity(requested) if requested else None
            if not requested or not requested_identity or requested_identity in visited_identities:
                raise ResearchError("Refusing to scrape a repeated or invalid page URL.")
            if not is_same_site(requested, homepage) or not is_public_http_url(requested):
                raise ResearchError("Requested page is not a public URL on the business website.")

            result = extract_page(requested, homepage, jina_reader, deadline)
            final_identity = url_identity(result["url"])
            if not final_identity or final_identity in visited_identities:
                raise ResearchError("Page redirected to a URL already scraped.")
            visited_identities.add(requested_identity)
            remember(result)

            page_record = {
                "url": result["url"],
                "content": result["content"],
                "links": result["links"],
                "emails": result["emails"],
                "summary": "",
                "relevant_findings": [],
                "agent_action": None,
                "requested_next_url": None,
                "request_reason": None,
            }
            pages.append(page_record)
            if on_page:
                on_page(pages, emails, list(discovered_links.values()))

        context = {
            "business": business,
            "current_page_url": result["url"],
            "cleaned_page_content": result["content"],
            "current_page_links": result["links"],
            "previously_scraped_pages": [
                {
                    "url": item["url"],
                    "summary": item["summary"],
                    "relevant_findings": item["relevant_findings"],
                    "agent_action": item["agent_action"],
                    "requested_next_url": item["requested_next_url"],
                    "request_reason": item["request_reason"],
                }
                for item in pages[:-1]
            ],
            "previously_discovered_emails": sorted(emails),
            "previously_discovered_links": list(discovered_links.values()),
            "available_links": [
                link
                for identity, link in discovered_links.items()
                if identity not in visited_identities
            ],
            "pages_scraped": len(pages),
            "pages_remaining": MAX_PAGES_PER_BUSINESS - len(pages),
            "visited_urls": sorted(visited_urls),
            "visited_identities": list(visited_identities),
        }
        out_of_time_now = out_of_time({"deadline": deadline}, PAGE_RESERVE_SECONDS)
        must_score = len(pages) >= MAX_PAGES_PER_BUSINESS or out_of_time_now
        note = TIME_LIMIT_NOTE if out_of_time_now and len(pages) < MAX_PAGES_PER_BUSINESS else PAGE_LIMIT_NOTE
        try:
            decision = validate_agent_response(client, model, context, must_score, system_prompt, score_note=note,
                                               deadline=deadline, output_fields=output_fields,
                                               answer_schema=answer_schema)
        except ResearchError:
            if must_score:
                raise
            # The agent could not pick a readable page: score from the pages read so far instead of failing.
            decision = validate_agent_response(client, model, context, True, system_prompt,
                                               score_note="no further useful page could be chosen", deadline=deadline,
                                               output_fields=output_fields, answer_schema=answer_schema)
        page_record.update(
            summary=decision["summary"],
            relevant_findings=decision["relevant_findings"],
            agent_action=decision["action"],
            requested_next_url=decision.get("url"),
            request_reason=decision.get("reason"),
        )
        if on_page:
            on_page(pages, emails, list(discovered_links.values()))
        if decision["action"] in {"SCORE", "EXIT"}:
            return {
                **decision,
                "pages_scraped": len(pages),
                "scraped_urls": [item["url"] for item in pages],
                "scraped_pages": pages,
                "discovered_links": list(discovered_links.values()),
                "emails": sorted(emails),
            }
        next_url = decision["url"]

    raise ResearchError("Research stopped without SCORE/EXIT at the five-page limit.")


@outside_event_loop
def start_profile(business_id: int) -> int:
    profile, _ = BusinessWebsiteProfile.objects.update_or_create(
        business_id=business_id,
        defaults={
            "status": "running",
            "pages_scraped": 0,
            "scraped_urls": [],
            "scraped_pages": [],
            "discovered_links": [],
            "emails": [],
            "qualification_score": None,
            "qualification_reasons": [],
            "agent_reasoning": None,
            "team_size": None,
            "languages": None,
            "extracted": None,
        },
    )
    return profile.pk


@outside_event_loop
def save_progress(profile_id: int, pages: list[dict], emails: set[str], discovered_links: list[dict]) -> None:
    BusinessWebsiteProfile.objects.filter(pk=profile_id).update(
        pages_scraped=len(pages),
        scraped_urls=[page["url"] for page in pages],
        scraped_pages=pages,
        discovered_links=discovered_links,
        emails=sorted(emails),
    )


BUSINESS_FIELDS = (
    "id", "name", "category", "website_url", "domain", "phone", "address", "city", "state", "country",
    "postal_code", "google_rating", "google_review_count",
)


ABANDONED_AFTER = timedelta(minutes=10)  # a research takes a few minutes (at most five pages)


def load_businesses(limit: int | None, business_ids: list[int] | None, retry_failed: bool = False,
                    redo: bool = False) -> list[dict]:
    eligible_statuses = ("pending", "failed") if retry_failed else ("pending",)
    queryset = Business.objects.filter(website_url__isnull=False).exclude(website_url="").order_by("id")
    # "running" for longer than a research can take: the process doing it died (crash, restart); research it again
    abandoned = Q(website_profile__status="running", website_profile__updated_at__lt=timezone.now() - ABANDONED_AFTER)
    if not redo:
        queryset = queryset.filter(Q(website_profile__isnull=True) | Q(website_profile__status__in=eligible_statuses)
                                   | abandoned)
    else:  # everything except a research that is running right now
        queryset = queryset.exclude(Q(website_profile__status="running") & ~abandoned)
    if business_ids:
        queryset = queryset.filter(id__in=business_ids)
    if limit is not None:
        queryset = queryset[:limit]
    return list(queryset.values(*BUSINESS_FIELDS, qualification_prompt=F("discovery_campaign__campaign_prompt__prompt"),
                                output_fields=F("discovery_campaign__campaign_prompt__output_fields"),
                                answer_schema=F("discovery_campaign__campaign_prompt__answer_schema")))


@outside_event_loop
def finalize_profile(profile_id: int, result: dict) -> None:
    BusinessWebsiteProfile.objects.filter(pk=profile_id).update(
        status="completed",
        pages_scraped=result["pages_scraped"],
        scraped_urls=result["scraped_urls"],
        scraped_pages=result["scraped_pages"],
        discovered_links=result["discovered_links"],
        emails=result["emails"],
        qualification_score=result["qualification_score"],
        qualification_reasons=result["qualification_reasons"],
        agent_reasoning=result["agent_reasoning"],
        extracted=result.get("extracted"),
        **known_columns(result.get("extracted")),
    )


def known_columns(extracted: dict | None) -> dict:
    """team_size and languages also get their own columns (filters, lists) when the output fields have them."""
    extracted = extracted or {}
    team_size = extracted.get("team_size")
    languages = extracted.get("languages")
    return {"team_size": team_size if isinstance(team_size, int) and not isinstance(team_size, bool) and team_size > 0
            else None,
            "languages": languages if isinstance(languages, list) and all(isinstance(x, str) for x in languages)
            else None}


@outside_event_loop
def interrupted_pages(business_id: int) -> list[dict]:
    """The pages an interrupted research of the business read (it is pending again or was left running); empty for
    one that never started, finished or failed."""
    profile = BusinessWebsiteProfile.objects.filter(business_id=business_id, status__in=("pending", "running")).first()
    return list(profile.scraped_pages or []) if profile else []


@outside_event_loop
def resume_profile(business_id: int) -> int:
    profile = BusinessWebsiteProfile.objects.get(business_id=business_id)
    BusinessWebsiteProfile.objects.filter(pk=profile.pk).update(status="running")
    return profile.pk


@outside_event_loop
def reset_profile(profile_id: int) -> None:
    """Back to pending, keeping the pages read: the research was interrupted (time limit), and the next run
    continues it."""
    BusinessWebsiteProfile.objects.filter(pk=profile_id).update(status="pending")


@outside_event_loop
def fail_profile(profile_id: int, error: Exception) -> None:
    BusinessWebsiteProfile.objects.filter(pk=profile_id).update(
        status="failed", agent_reasoning=f"{type(error).__name__}: {error}"[:12000]
    )


def add_arguments(parser) -> None:
    parser.add_argument("--business-id", type=int, action="append", help="Research this business only (repeatable).")
    parser.add_argument("--limit", type=int, help="Maximum number of businesses in this run.")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Include businesses whose previous website research failed.",
    )
    parser.add_argument(
        "--redo",
        action="store_true",
        help="Research again businesses that were already researched (replaces their score and findings).",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Do not send page URLs to Jina Reader; read only the pages' HTML (sites built by JavaScript show less).",
    )


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    llm = make_llm_client()
    if llm is None:
        raise CommandError("Set GROQ_API_KEY (or GROK_API_KEY) or OPENAI_API_KEY in the environment or project .env file")
    client, model, provider = llm
    print(f"Using {provider} model {model}.")
    deadline = options.get("deadline")
    jina_reader = None if options["local_only"] else JinaReader(max_wait=30 if deadline is not None else None)

    queue = load_businesses(options["limit"], options["business_id"], retry_failed=options["retry_failed"],
                            redo=options["redo"])
    if not queue:
        print("No businesses with websites need research. Businesses already researched are skipped; add --redo to "
              "research them again (--retry-failed retries only failed ones).")
        return 0

    unprompted = [business for business in queue if not (business["qualification_prompt"] or "").strip()]
    queue = [business for business in queue if business not in unprompted]
    for business in unprompted:
        print(f"Business {business['id']} ({business['name'] or 'unnamed'}) skipped: its campaign has no campaign prompt "
              "(the qualification criteria). Add one to the campaign in the admin.")
    if not queue:
        raise CommandError("No business could be researched: their campaigns have no campaign prompt.")

    print(f"Researching {len(queue)} businesses; maximum {MAX_PAGES_PER_BUSINESS} pages each.")
    completed = 0
    failed = len(unprompted)
    for position, business in enumerate(queue):
        if out_of_time(options, BUSINESS_RESERVE_SECONDS):
            time_limit_note(len(queue) - position, "business(es)")
            break
        saved = [] if options["redo"] else interrupted_pages(business["id"])
        profile_id = resume_profile(business["id"]) if saved else start_profile(business["id"])
        print(f"Researching business {business['id']}: {business['name'] or '(unnamed)'}"
              + (f" (continuing after {len(saved)} page(s) read before)" if saved else ""), flush=True)
        try:
            save_page = lambda pages, emails, links: save_progress(profile_id, pages, emails, links)
            result = research_business(client, model, business, jina_reader, on_page=save_page, deadline=deadline,
                                       saved_pages=saved)
            finalize_profile(profile_id, result)
            completed += 1
            score = result.get("qualification_score")
            print(f"Business {business['id']}: {result['action']} after {result['pages_scraped']} page(s)"
                  + (f", score {score}." if score is not None else "."))
        except OutOfTime as error:  # not the website's fault: the next run researches it again
            reset_profile(profile_id)
            print(f"Business {business['id']}: stopped, {error}; it is researched again in the next run.")
            time_limit_note(len(queue) - position, "business(es)")
            break
        except Exception as error:  # noqa: BLE001 - one website must not stop the others
            fail_profile(profile_id, error)
            failed += 1
            print(f"Business {business['id']} failed ({type(error).__name__}): {error}")
    print(f"Research complete: {completed} completed, {failed - len(unprompted)} failed"
          + (f", {len(unprompted)} skipped (no campaign prompt)." if unprompted else "."))
    return PARTIAL if failed else 0
