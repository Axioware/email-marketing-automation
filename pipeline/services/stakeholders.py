"""Module 3: find the decision makers of qualified businesses and the email addresses they probably use.

Waterfall: Module 2's website data, then the business's own team/about/contact pages (read through Jina Reader, which
renders them in its own browser), then web search through your SearXNG instance (SEARXNG_URL). Rules only, no LLM.
No browser runs here, so the step runs on the web app (Vercel) as well. Email candidates are stored for Module 4 to
verify.
"""
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.db.models import F, Q

from pipeline.management.base import OutOfTime, call_timeout, out_of_time, time_limit_note
from pipeline.models import Business, BusinessContact, BusinessWebsiteProfile
from pipeline.services import qualify, searxng
from pipeline.services.research import JINA_TIMEOUT, JinaReader, ResearchError, is_public_http_url

DEFAULT_ROLES_FILE = Path(settings.BASE_DIR) / "config" / "target_roles_dental.json"
MAX_PAGE_TEXT_CHARS = 8000
MAX_CONTACTS_PER_BUSINESS = 4
MAX_FALLBACK_CONTACTS = 2
MAX_DOMAINS = 2
MAX_TITLE_CHARS = 80
MAX_SEARCH_RESULTS = 10  # results read per search, best first
# With a deadline (the web app on Vercel): a business starts only with this much time left; site pages and searches
# stop when less than these are left.
BUSINESS_RESERVE_SECONDS = 60
PAGE_RESERVE_SECONDS = 40
SEARCH_RESERVE_SECONDS = 25
OWN_EMAIL_SOURCES = ("website", "search", "inferred", "pattern")  # rows this module may replace

HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "sir", "mx", "eng", "engr"}
CREDENTIALS = {
    "jr", "sr", "ii", "iii", "iv", "md", "dds", "dmd", "bds", "mbbs", "phd", "esq", "cpa", "msc", "bsc",
    "ms", "rdh", "fcps", "mds", "mph",
}
FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com"}
ROLE_BASED_LOCAL_PARTS = {
    "info", "contact", "admin", "office", "hello", "hi", "support", "sales", "appointments", "appointment",
    "reception", "frontdesk", "booking", "bookings", "enquiries", "enquiry", "inquiries", "inquiry",
    "mail", "team", "billing", "hr", "careers", "service", "services", "care", "help", "dr", "doctor",
    "clinic", "dental", "webmaster", "marketing", "accounts", "noreply", "no-reply", "feedback",
}
NOT_NAME_WORDS = {
    "the", "this", "that", "google", "maps", "overview", "ai", "inc", "llc", "ltd", "company", "business",
    "owner", "owners", "founder", "manager", "director", "partner", "ceo", "president", "january",
    "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "according",
    "based", "however", "while", "also", "he", "she", "they", "co", "managing", "general", "chief",
    "executive", "officer", "current", "its", "our", "meet", "team", "staff", "doctors", "doctor",
    "dentist", "dentists", "contact", "us", "about", "home", "book", "now", "call", "today", "welcome",
    "why", "choose", "best", "top", "new", "patient", "patients", "review", "reviews", "testimonial",
    "testimonials", "read", "more", "view", "all", "learn", "get", "free", "leadership", "office",
    "practice", "clinic", "front", "desk", "associate", "lead", "senior", "head", "and", "of", "in", "at",
    "search", "assist", "results", "result", "found", "no", "duckduckgo", "images", "videos", "news",
    "shopping", "settings", "privacy", "sign", "login", "for",
}
BUSINESS_WORDS = {
    "solutions", "dental", "dentistry", "clinics", "services", "service", "group", "center", "centre",
    "associates", "studio", "studios", "care", "smile", "smiles", "health", "healthcare", "family",
    "salon", "spa", "shop", "store", "restaurant", "cafe", "repair", "plumbing", "law", "firm", "agency",
    "consulting", "partners", "enterprises", "holdings", "corp", "corporation", "orthodontics",
    "wellness", "medical", "hospital", "pharmacy", "gym", "implants", "braces", "whitening", "cosmetic",
}
BUSINESS_NAME_STOPWORDS = BUSINESS_WORDS | NOT_NAME_WORDS | {"mall", "plaza", "city", "street", "road"}

# (label, template, confidence for a pure guess), most to least common for small businesses.
# Placeholders: {first} {last} {f} first initial, {l} last initial.
EMAIL_PATTERNS = [
    ("first", "{first}", 0.30),
    ("first.last", "{first}.{last}", 0.30),
    ("flast", "{f}{last}", 0.20),
    ("firstlast", "{first}{last}", 0.15),
    ("f.last", "{f}.{last}", 0.10),
    ("first_last", "{first}_{last}", 0.10),
    ("last", "{last}", 0.05),
    ("first-last", "{first}-{last}", 0.05),
    ("firstl", "{first}{l}", 0.05),
    ("dr.last", "dr.{last}", 0.04),
    ("last.first", "{last}.{first}", 0.04),
    ("drlast", "dr{last}", 0.03),
    ("dr.first", "dr.{first}", 0.03),
    ("first.l", "{first}.{l}", 0.03),
    ("lastfirst", "{last}{first}", 0.03),
    ("lastf", "{last}{f}", 0.03),
    ("f_last", "{f}_{last}", 0.03),
    ("f-last", "{f}-{last}", 0.02),
    ("last_first", "{last}_{first}", 0.02),
    ("drfirst", "dr{first}", 0.02),
    ("fl", "{f}{l}", 0.01),
]
ALTERNATE_NAME_WEIGHT = 0.6  # middle given names and parts of hyphenated surnames
PREFIX_NAME_WEIGHT = 0.3  # honorific-like names (Muhammad, Syed...) that people rarely use in email
PREFIX_NAMES = {
    "muhammad", "mohammad", "mohammed", "muhammed", "mohamed", "muhamad", "mohd", "md", "syed", "sayyid",
    "sayed", "hafiz", "qari", "mian", "malik", "raja", "sheikh", "shaikh", "ch", "chaudhry", "chaudhary",
    "choudhry", "khawaja", "mir", "sardar", "sahibzada", "maulana",
}
SOURCE_BASE_CONFIDENCE = {"website": 0.80, "search_ai": 0.65, "search_snippet": 0.50}
INFERRED_EMAIL_FACTOR = 0.50
INFERRED_OBSERVED_PATTERN_FACTOR = 0.70
SECONDARY_DOMAIN_FACTOR = 0.5

NAME = (
    r"(?:(?:Dr|Mr|Mrs|Ms|Prof|Engr)\.?[ \t]+(?:[A-Z]\.\s*)*[A-Z][A-Za-z'’\-]+(?:[ \t]+(?:[A-Z]\.|[A-Z][A-Za-z'’\-]+)){0,3}"
    r"|[A-Z][A-Za-z'’\-]+(?:[ \t]+(?:[A-Z]\.|[A-Z][A-Za-z'’\-]+)){1,3})"
)
CREDENTIAL_SUFFIX = r"(?:,?\s+(?:DDS|DMD|BDS|MBBS|MDS|MD|PhD|FCPS|MSc|BSc|MS|RDH|Jr\.?|Sr\.?|III|II))*"
NAME_LIST = rf"{NAME}(?:\s*(?:,\s*and|,|\band|&)\s*{NAME})*"
VERB_TITLES = {
    "owned": "owner", "founded": "founder", "co-founded": "founder", "run": "owner",
    "operated": "owner", "managed": "manager", "led": "director",
}
SITE_PAGE_KEYWORDS = [  # priority order for choosing pages worth rendering
    "team", "doctor", "dentist", "staff", "meet", "leadership", "people", "about", "who-we-are",
    "our-story", "clinic", "contact",
]
COMMON_SITE_PATHS = [
    "/about", "/about-us", "/team", "/our-team", "/meet-the-doctors", "/meet-our-team", "/our-doctors",
    "/doctors", "/leadership", "/staff", "/contact", "/contact-us",
]
MIN_RENDERED_TEXT_CHARS = 80
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class StakeholderError(RuntimeError):
    pass


# ---------------------------------------------------------------- configuration


class RoleConfig:
    def __init__(self, data: dict):
        self.targets = data["target_roles"]
        self.fallbacks = data.get("fallback_roles", [])
        self.excludes = [self._norm(item) for item in data.get("exclude_keywords", [])]
        keywords = {self._norm(k) for role in self.targets + self.fallbacks for k in role["keywords"]}
        keywords |= set(self.excludes)
        alternatives = "|".join(
            re.escape(word).replace(r"\ ", r"[\s\-/]+") for word in sorted(keywords, key=len, reverse=True)
        )
        self.keyword_pattern = f"(?i:{alternatives})"

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"[\s\-/]+", " ", value.casefold()).strip()

    def _matches(self, title: str, keywords: list[str]) -> bool:
        padded = f" {self._norm(title)} "
        return any(f" {self._norm(word)} " in padded for word in keywords)

    def classify(self, title: str) -> dict | None:
        """Return {kind, rank, role_name, role_type}; kind is target, fallback or excluded."""
        for rank, role in enumerate(self.targets):
            if self._matches(title, role["keywords"]):
                return {"kind": "target", "rank": rank, "role_name": role["name"], "role_type": role["role_type"]}
        if self._matches(title, self.excludes):
            return {"kind": "excluded", "rank": 99, "role_name": None, "role_type": "other"}
        for role in self.fallbacks:
            if self._matches(title, role["keywords"]):
                return {"kind": "fallback", "rank": 50, "role_name": role["name"], "role_type": role["role_type"]}
        return None


def load_role_config(path: Path) -> RoleConfig:
    with open(path, encoding="utf-8") as handle:
        return RoleConfig(json.load(handle))


# ---------------------------------------------------------------- names and emails


def ascii_token(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", folded.casefold())


def clean_name(raw: str) -> dict | None:
    """Return {name, first_name, last_name} or None when the text is not a plausible person name."""
    tokens = raw.replace("’", "'").replace(",", " ").split()
    for index, token in enumerate(tokens):
        if token.casefold().endswith("'s"):
            if index < len(tokens) - 1:  # "Sonal Shah's Smile Solutions" is a business, not a person
                return None
            tokens[index] = token[:-2]
    while tokens and ascii_token(tokens[-1]) in CREDENTIALS | NOT_NAME_WORDS:
        tokens.pop()
    had_honorific = False
    while tokens and (tokens[0].rstrip(".").casefold() in HONORIFICS or ascii_token(tokens[0]) in NOT_NAME_WORDS):
        had_honorific = had_honorific or tokens[0].rstrip(".").casefold() in HONORIFICS
        tokens.pop(0)
    while len(tokens) > (1 if had_honorific else 2) and re.fullmatch(r"[A-Z]\.?", tokens[0]):
        tokens.pop(0)  # "Dr. M. Shoaib Ahmed" -> "Shoaib Ahmed"
    if len(tokens) < (1 if had_honorific else 2):
        return None
    keys = [ascii_token(token) for token in tokens]
    if not all(keys) or any(key in NOT_NAME_WORDS or key in BUSINESS_WORDS for key in keys):
        return None
    return {
        "name": " ".join(tokens)[:255],
        "first_name": display_first_name(tokens)[:120],
        "last_name": tokens[-1][:120] if len(tokens) > 1 else None,
    }


def person_key(first_name: str, last_name: str | None) -> str:
    return f"{ascii_token(first_name)}|{ascii_token(last_name or '')}"


def host_of(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    parsed = urlsplit(value if "//" in value else f"//{value}")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    return host if "." in host and host not in FREE_EMAIL_DOMAINS else None


def collect_domains(business: dict) -> list[str]:
    """Business domain, website host and scraped-page hosts. Email domains are not trusted."""
    domains = []
    for value in [business.get("domain"), business.get("website_url"), *(business.get("scraped_urls") or [])]:
        host = host_of(value)
        if host and host not in domains:
            domains.append(host)
    return domains[:MAX_DOMAINS]


def significant_tokens(name: str) -> list[str]:
    tokens = [token for token in name.replace("\u2019", "'").split() if token]
    return [t for t in tokens if t.rstrip(".").casefold() not in HONORIFICS and not re.fullmatch(r"[A-Za-z]\.?", t)]


def display_first_name(tokens: list[str]) -> str:
    """The given name people actually go by: skips a leading Muhammad/Syed-style prefix when others follow."""
    if len(tokens) > 2 and ascii_token(tokens[0]) in PREFIX_NAMES:
        return tokens[1]
    return tokens[0]


def name_variants(name: str) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """(given names, surnames) as (ascii token, weight).

    A leading prefix name (Muhammad, Syed...) is kept only as a low-weight alternate, since people
    rarely use it in email: "Muhammad Shoaib Ahmed" is shoaib@ first, muhammad@ only as a long shot.
    Other middle names and the parts of hyphenated surnames are medium-weight alternates.
    """
    tokens = significant_tokens(name)
    if not tokens:
        return [], []
    givens: list[tuple[str, float]] = []
    lasts: list[tuple[str, float]] = []
    if len(tokens) > 1 and ascii_token(tokens[0]) in PREFIX_NAMES:
        rest = tokens[1:]
        givens.append((ascii_token(rest[0]), 1.0))
        if len(rest) > 1:
            givens += [(ascii_token(t), ALTERNATE_NAME_WEIGHT) for t in rest[1:-1]]
            surname_token = rest[-1]
        else:
            surname_token = rest[0]  # "Muhammad Ali": Ali is both the given name and the surname
        givens.append((ascii_token(tokens[0]), PREFIX_NAME_WEIGHT))
    else:
        givens.append((ascii_token(tokens[0]), 1.0))
        surname_token = tokens[-1] if len(tokens) > 1 else None
        if len(tokens) > 2:
            givens += [
                (ascii_token(t), PREFIX_NAME_WEIGHT if ascii_token(t) in PREFIX_NAMES else ALTERNATE_NAME_WEIGHT)
                for t in tokens[1:-1]
            ]
    if surname_token:
        lasts.append((ascii_token(surname_token), 1.0))
        parts = [ascii_token(part) for part in re.split(r"[-']", surname_token)]
        if len(parts) > 1:
            lasts += [(part, ALTERNATE_NAME_WEIGHT) for part in parts if part]
    unique_givens, unique_lasts, seen = [], [], set()
    for token, weight in givens:
        if token and token not in seen:
            seen.add(token)
            unique_givens.append((token, weight))
    seen = set()
    for token, weight in lasts:
        if token and token not in seen:
            seen.add(token)
            unique_lasts.append((token, weight))
    return unique_givens, unique_lasts


def candidate_emails(name: str, domain: str | None) -> list[tuple[str, str, float]]:
    """(pattern_label, address, weight) for every pattern x name variant. Without a surname only
    first-name patterns are possible. Ordered by pattern popularity, primary name variant first."""
    if not domain:
        return []
    givens, lasts = name_variants(name)
    results: dict[str, tuple[str, float, int]] = {}
    for order, (label, template, _) in enumerate(EMAIL_PATTERNS):
        needs_last = "{last}" in template or "{l}" in template
        for first, first_weight in givens:
            for last, last_weight in (lasts or [("", 1.0)]):
                if needs_last and (not last or last == first):
                    continue
                address = template.format(first=first, last=last, f=first[:1], l=last[:1]) + f"@{domain}"
                weight = first_weight * (last_weight if needs_last else 1.0)
                if address not in results or weight > results[address][1]:
                    results[address] = (label, weight, order)
    base = {label: confidence for label, _, confidence in EMAIL_PATTERNS}
    ordered = sorted(results.items(), key=lambda item: (-base[item[1][0]] * item[1][1], item[1][2]))
    return [(label, address, weight) for address, (label, weight, _) in ordered]


def is_role_based(address: str) -> bool:
    local = address.partition("@")[0].casefold()
    return local in ROLE_BASED_LOCAL_PARTS or ascii_token(local) in ROLE_BASED_LOCAL_PARTS


def email_pattern_for(person: dict, address: str) -> str | None:
    """Return the pattern label when this found address plausibly belongs to the person."""
    domain = address.casefold().partition("@")[2]
    for label, candidate, _ in candidate_emails(person["name"], domain):
        if candidate == address.casefold():
            return label
    return None


# ---------------------------------------------------------------- text extraction


def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = text.replace("|", ", ")
    text = re.sub(r"[#*_`>]+", " ", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def tidy_title(title: str, keyword_pattern: str | None = None) -> str:
    if keyword_pattern:
        matches = list(re.finditer(keyword_pattern, title[:60]))
        if matches:
            title = title[: matches[-1].end()]
            first = matches[0].start()
            words_before = title[:first].split()
            if len(words_before) > 2:
                title = " ".join(words_before[-2:]) + " " + title[first:]
    title = re.split(r",\s+(?:and\s+)?[A-Z][a-z]+\s+[A-Z]|\s+and\s+[A-Z][a-z]+\s+[A-Z][a-z]+", title)[0]
    title = re.sub(r"^(?:the|a|an|our|its|current|lead)\s+", "", title.strip(" ,.;:-–—"), flags=re.I)
    title = title.strip(" ,.;:-–—")
    return (title[:1].upper() + title[1:])[:MAX_TITLE_CHARS]


def sentence_around(text: str, start: int, end: int) -> str:
    """The line holding the match (snippets and paragraphs are one line each), capped around the match."""
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    right = len(text) if right == -1 else right
    if right - left > 500:
        left, right = max(left, start - 250), min(right, end + 250)
    return " ".join(text[left:right].split())


def extract_people(text: str, roles: RoleConfig, business_name: str = "") -> list[dict]:
    """Find (name, title) pairs in free text or team-page markdown. Excluded roles are dropped."""
    text = clean_markdown(text)
    kw = roles.keyword_pattern
    name_then_title = re.compile(
        rf"(?P<name>{NAME}){CREDENTIAL_SUFFIX}(?:\s*\([^)]*\))?\s*"
        rf"(?:,|\bis\b|\bare\b|\bwas\b|\bserves as\b|\bas\b|–|—|-|:)\s*(?P<title>[^.\n;]{{1,60}})"
    )
    title_then_name = re.compile(
        rf"(?P<title>(?:[A-Za-z\-]+\s+){{0,2}}?{kw})s?\s*(?:\bis\b|\bare\b|\bwas\b|:|-|–)\s*(?P<names>{NAME_LIST})"
    )
    verb_then_name = re.compile(
        rf"(?P<verb>(?i:owned|founded|co-founded|run|operated|managed|led))\s+(?i:by)\s+(?P<names>{NAME_LIST})"
    )
    found = []

    def add(raw_names: str, title: str, evidence: str) -> None:
        classification = roles.classify(title)
        if not classification or classification["kind"] == "excluded":
            return
        for raw in re.split(r"\s*(?:,\s*and\s+|,|\band\s+|&)\s*", raw_names):
            person = clean_name(raw)
            if person:
                found.append({**person, "job_title": tidy_title(title, kw), **classification, "evidence": evidence})

    for match in name_then_title.finditer(text):
        if re.search(kw, match.group("title")[:45]):
            add(match.group("name"), match.group("title"), sentence_around(text, match.start(), match.end()))
    for match in title_then_name.finditer(text):
        add(match.group("names"), match.group("title"), sentence_around(text, match.start(), match.end()))
    for match in verb_then_name.finditer(text):
        add(match.group("names"), VERB_TITLES[match.group("verb").casefold()],
            sentence_around(text, match.start(), match.end()))

    # "Dr. Jane Smith & Associates": the practice is named after its principal.
    owner_rank = next((i for i, role in enumerate(roles.targets) if role["name"] == "Dentist Owner"), 0)
    for match in re.finditer(rf"(?P<name>{NAME}){CREDENTIAL_SUFFIX}\s*(?:&|and)\s*Associates\b", text):
        person = clean_name(match.group("name"))
        if person and re.match(r"(?:Dr|Prof)\b", match.group("name")):
            role = roles.targets[owner_rank]
            found.append(
                {
                    **person,
                    "job_title": "Principal (practice named after them)",
                    "kind": "target",
                    "rank": owner_rank,
                    "role_name": role["name"],
                    "role_type": role["role_type"],
                    "evidence": sentence_around(text, match.start(), match.end()),
                    "weak": True,
                }
            )

    # "Dr. Kashif Jamal" at "Kashif Dental Clinic": the practice carries its principal's name.
    business_tokens = distinctive_tokens(business_name)
    if business_tokens:
        owner_rank = next((i for i, role in enumerate(roles.targets) if role["name"] == "Dentist Owner"), 0)
        for match in re.finditer(rf"\b(?:Dr|Prof)\.?[ \t]+(?:[A-Z]\.[ \t]*)*[A-Z][A-Za-z'\u2019\-]+(?:[ \t]+[A-Z][A-Za-z'\u2019\-]+){{0,2}}", text):
            person = clean_name(match.group(0))
            if person and business_tokens & {ascii_token(t) for t in person["name"].split()}:
                role = roles.targets[owner_rank]
                found.append(
                    {
                        **person,
                        "job_title": "Principal (practice named after them)",
                        "kind": "target",
                        "rank": owner_rank,
                        "role_name": role["name"],
                        "role_type": role["role_type"],
                        "evidence": sentence_around(text, match.start(), match.end()),
                        "weak": True,
                    }
                )

    # Team-page layout: a name on one line and the job title on the next line(s).
    lines = [line for line in text.splitlines() if line.strip()]
    name_line = re.compile(rf"^{NAME}{CREDENTIAL_SUFFIX}$")
    for index, line in enumerate(lines):
        if len(line.split()) > 7 or not name_line.match(line.strip()):
            continue
        for follow in lines[index + 1:index + 3]:
            if len(follow) > 90:
                break
            if re.search(kw, follow):
                add(line.strip(), follow, f"{line.strip()} - {follow.strip()}"[:500])
                break
            if name_line.match(follow.strip()):
                break
    return found


def search_name(business_name: str) -> str:
    """The business's name without the branch or location Google Maps adds ("Alvi Dental Hospital — SMCHS",
    "Smile Studio | DHA", "City Dental (Gulberg)"): web pages about the business rarely repeat that part."""
    name = " ".join((business_name or "").split())
    for separator in (" — ", " – ", " | ", " ("):
        name = name.split(separator)[0]
    head, _, tail = name.rpartition(" - ")
    if head and len(tail.split()) <= 3:
        name = head
    return name.strip() or " ".join((business_name or "").split())


def distinctive_tokens(business_name: str) -> set[str]:
    tokens = {ascii_token(word) for word in business_name.split()}
    return {token for token in tokens if len(token) > 2 and token not in BUSINESS_NAME_STOPWORDS}


def phone_key(phone: str | None) -> str | None:
    """Last 9 digits of a phone number, enough to match it across formats (+92 300 ..., 0300-...)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 9 else None


def mentions_business(
    text: str, business_name: str, context_tokens: set[str] = frozenset(), phone: str | None = None
) -> bool:
    """True when the text is plausibly about this business, not another one with a similar name.

    The business's own phone number is conclusive. Otherwise the name must match, and a name with
    no distinctive words (e.g. "Smile Solutions") must also match the location or full domain.
    """
    if phone and phone in re.sub(r"\D", "", text):
        return True
    tokens = distinctive_tokens(business_name)
    haystack = ascii_token(text)
    if not tokens:  # generic name: need the exact name AND the business's location/domain
        return ascii_token(business_name) in haystack and any(token in haystack for token in context_tokens)
    return sum(1 for token in tokens if token in haystack) >= min(2, len(tokens))


def business_location(business: dict) -> str | None:
    """City, or the last address part before the postal code and country (e.g. 'Lahore')."""
    if business.get("city"):
        return business["city"]
    country = (business.get("country") or "").casefold()
    parts = [part.strip() for part in (business.get("address") or "").split(",") if part.strip()]
    parts = [part for part in parts if part.casefold() != country and not re.search(r"\d", part)]
    return parts[-1] if parts else None


def context_tokens_for(business: dict, domains: list[str]) -> set[str]:
    tokens = {ascii_token(business_location(business) or "")}
    tokens |= {ascii_token(domain) for domain in domains}  # full domain: the bare name alone proves nothing
    return {token for token in tokens if len(token) > 3}


def linkedin_slug_url(url: str) -> str | None:
    match = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%.]+)", url or "")
    return f"https://www.linkedin.com/in/{match.group(1).rstrip('/')}" if match else None


# ---------------------------------------------------------------- evidence aggregation


class Evidence:
    """People, emails and LinkedIn URLs gathered across the waterfall, merged by person."""

    def __init__(
        self, business_name: str, domains: list[str], context_tokens: set[str] = frozenset(), phone: str | None = None
    ):
        self.business_name = business_name
        self.domains = domains
        self.context_tokens = context_tokens
        self.phone = phone
        self.people: dict[str, dict] = {}
        self.emails: dict[str, dict] = {}
        self.linkedin: dict[str, str] = {}

    def add_people(self, found: list[dict], source: str, url: str, require_mention: bool = False) -> None:
        for person in found:
            if require_mention and not mentions_business(person["evidence"], self.business_name, self.context_tokens, self.phone):
                continue
            key = person_key(person["first_name"], person["last_name"])
            entry = self.people.get(key) or self._variant_of(person)
            if entry is None:
                entry = {**person, "sources": {}, "evidences": []}
                self.people[key] = entry
            elif (person["kind"] == "target", -person["rank"]) > (entry["kind"] == "target", -entry["rank"]):
                for field in ("job_title", "kind", "rank", "role_name", "role_type"):
                    entry[field] = person[field]
            if len(person["name"]) > len(entry["name"]):
                entry["name"] = person["name"]
            entry["sources"].setdefault(url, source)
            if person["evidence"] and person["evidence"] not in entry["evidences"]:
                entry["evidences"].append(person["evidence"])

    def _variant_of(self, person: dict) -> dict | None:
        """An existing person whose name tokens are a prefix of (or extend) this one, e.g. 'Muhammad Shoaib'."""
        tokens = [ascii_token(t) for t in person["name"].split()]
        for entry in self.people.values():
            other = [ascii_token(t) for t in entry["name"].split()]
            short, long_ = sorted((tokens, other), key=len)
            if len(short) >= 2 and (
                long_[: len(short)] == short or (short[-1] == long_[-1] and short[0] in long_)
            ):
                return entry
        return None

    def add_emails(self, addresses, source: str, url: str) -> None:
        for address in addresses:
            address = address.strip().casefold()
            if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", address):
                continue
            entry = self.emails.setdefault(address, {"source": source, "urls": []})
            if url and url not in entry["urls"]:
                entry["urls"].append(url)

    def add_linkedin(self, urls) -> None:
        for url in urls:
            profile = linkedin_slug_url(url)
            if profile:
                self.linkedin.setdefault(ascii_token(profile.rsplit("/", 1)[-1]), profile)

    def has_target(self) -> bool:
        return any(person["kind"] == "target" for person in self.people.values())

    def linkedin_for(self, person: dict) -> str | None:
        first, last = ascii_token(person["first_name"]), ascii_token(person["last_name"] or "")
        if not last:
            return None
        for slug, profile in self.linkedin.items():
            if first in slug and last in slug:
                return profile
        return None

    def discovered_email_for(self, person: dict) -> tuple[str, str, str, list[str]] | None:
        """(address, source, pattern, urls) for a found, non-role-based email belonging to the person."""
        for address, info in self.emails.items():
            domain = address.partition("@")[2]
            if is_role_based(address) or (self.domains and not self._domain_ok(domain)):
                continue
            pattern = email_pattern_for(person, address)
            if pattern:
                return address, info["source"], pattern, info["urls"]
        return None

    def _domain_ok(self, domain: str) -> bool:
        return any(domain == own or domain.endswith(f".{own}") for own in self.domains)


# ---------------------------------------------------------------- contact building


def person_confidence(person: dict, linkedin: str | None) -> float:
    base = max(SOURCE_BASE_CONFIDENCE[source] for source in person["sources"].values())
    if len(person["sources"]) > 1:
        base += 0.10
    if person["kind"] == "target" and person["rank"] == 0:
        base += 0.05
    if linkedin:
        base += 0.05
    if person["kind"] == "fallback":
        base *= 0.7
    if person.get("weak"):
        base *= 0.85
    return min(base, 0.95)


def build_contacts(business_id: int, evidence: Evidence) -> list[dict]:
    """Rank people by the role hierarchy, pick a primary, and attach discovered or inferred emails."""
    people = list(evidence.people.values())
    targets = [p for p in people if p["kind"] == "target"]
    chosen = targets or [p for p in people if p["kind"] == "fallback"][:MAX_FALLBACK_CONTACTS]
    if not chosen:
        return []

    discovered = {id(p): evidence.discovered_email_for(p) for p in chosen}
    pattern_names = {name for name, _, _ in EMAIL_PATTERNS}
    observed_pattern = next((f[2] for f in discovered.values() if f and f[2] in pattern_names), None)
    pattern_confidence = {name: confidence for name, _, confidence in EMAIL_PATTERNS}

    contacts = []
    for person in chosen:
        linkedin = evidence.linkedin_for(person)
        confidence = person_confidence(person, linkedin)
        source_urls = list(person["sources"])
        if linkedin and linkedin not in source_urls:
            source_urls.append(linkedin)

        candidates = []
        for domain_index, domain in enumerate(evidence.domains):
            options = candidate_emails(person["name"], domain)
            if observed_pattern:
                options.sort(key=lambda item: item[0] != observed_pattern)  # stable: keeps variant order
            for pattern_name, address, weight in options:
                base = INFERRED_OBSERVED_PATTERN_FACTOR if pattern_name == observed_pattern else pattern_confidence[pattern_name]
                candidates.append(
                    {
                        "email": address,
                        "pattern": pattern_name,
                        "confidence": round(base * weight * (SECONDARY_DOMAIN_FACTOR if domain_index else 1), 3),
                    }
                )

        found = discovered[id(person)]
        if found:
            email, email_source, _, email_urls = found
            source_urls += [url for url in email_urls if url not in source_urls]
            candidates = [item for item in candidates if item["email"] != email]
        elif candidates:
            email, email_source = candidates[0]["email"], "inferred"
            confidence *= INFERRED_OBSERVED_PATTERN_FACTOR if observed_pattern else INFERRED_EMAIL_FACTOR
        else:
            email, email_source = None, None

        contacts.append(
            {
                "business_id": business_id,
                "name": person["name"],
                "first_name": person["first_name"],
                "last_name": person["last_name"],
                "job_title": person["job_title"] or person["role_name"],
                "role_type": person["role_type"],
                "email": email,
                "email_source": email_source,
                "email_status": "unverified" if email else None,
                "linkedin_url": linkedin,
                "source_urls": source_urls,
                "confidence": round(confidence, 3),
                "candidate_emails": candidates,
                "_rank": (person["kind"] != "target", person["rank"], -confidence, email is None),
                "_evidence": " | ".join(person["evidences"][:3]),
                "_role_name": person["role_name"],
                "_kind": person["kind"],
            }
        )

    contacts.sort(key=lambda row: row["_rank"])
    contacts = contacts[:MAX_CONTACTS_PER_BUSINESS]
    primary = contacts[0]
    for index, row in enumerate(contacts):
        row["is_primary"] = index == 0
        if index == 0:
            why = f"Selected as primary contact: {row['_role_name']} is the highest-ranked role found"
            if row["_kind"] == "fallback":
                why = "Selected as primary contact: no target role was found, so the best dentist/doctor was used"
            if len(contacts) > 1:
                why += "; retained as secondary: " + ", ".join(f"{c['name']} ({c['job_title']})" for c in contacts[1:])
        else:
            why = f"Secondary contact ({row['_role_name']}); primary is {primary['name']}"
        row["discovery_reasoning"] = f"{why}. Evidence: {row['_evidence']}"[:4000]
    for row in contacts:
        for private in [key for key in row if key.startswith("_")]:
            del row[private]
    return contacts


# ---------------------------------------------------------------- site pages and search


def page_identity(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return (parsed.hostname or "").casefold().removeprefix("www."), parsed.path.rstrip("/").casefold() or "/"


def same_site(url: str, base_host: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    return bool(host) and (host == base_host or host.endswith(f".{base_host}"))


def choose_site_pages(business: dict, max_pages: int) -> list[str]:
    """Team/about/contact pages worth rendering: links Module 2 saw first, then common paths.

    Pages Module 2 already read with real content are skipped.
    """
    website = (business.get("website_url") or "").strip()
    if not website:
        return []
    if "//" not in website:
        website = f"http://{website}"
    base = urlsplit(website)
    base_host = (base.hostname or "").casefold().removeprefix("www.")
    if not base_host:
        return []
    done = {
        page_identity(page["url"])
        for page in business.get("scraped_pages") or []
        if page.get("url") and len(page.get("content") or "") > 300
    }
    links = [link.get("url", "") for link in business.get("discovered_links") or []]
    for page in business.get("scraped_pages") or []:
        links += [link.get("url", "") for link in page.get("links") or []]

    def priority(url: str) -> int:
        path = urlsplit(url).path.casefold()
        return next(
            (i for i, word in enumerate(SITE_PAGE_KEYWORDS) if re.search(rf"(?<![a-z]){word}s?(?![a-z])", path)),
            len(SITE_PAGE_KEYWORDS),
        )

    ranked = sorted(
        {page_identity(u): u for u in links if u.startswith("http") and same_site(u, base_host)}.values(),
        key=priority,
    )
    candidates = [u for u in ranked if priority(u) < len(SITE_PAGE_KEYWORDS)]
    candidates += [f"{base.scheme}://{base.netloc}{path}" for path in COMMON_SITE_PATHS]
    chosen, seen = [], set(done)
    for url in [f"{base.scheme}://{base.netloc}/", *candidates]:
        identity = page_identity(url)
        if identity not in seen:
            seen.add(identity)
            chosen.append(url)
    return chosen[:max_pages]


def crawl_site(reader: JinaReader, business: dict, evidence: Evidence, roles: RoleConfig, max_pages: int,
               options: dict | None = None) -> int:
    """Waterfall step 2: read the site's team/about/contact pages as a visitor sees them (Jina Reader renders them)."""
    base_host = host_of(business.get("website_url")) or ""
    rendered = 0
    read = set()  # final pages already read: a common path (e.g. /about) often redirects to one read before
    for url in choose_site_pages(business, max_pages):
        if evidence.has_target() or out_of_time(options or {}, PAGE_RESERVE_SECONDS):
            break
        if not is_public_http_url(url):
            continue  # never let a business's website point us at this machine or the local network
        try:
            page = reader.read(url, call_timeout((options or {}).get("deadline"), JINA_TIMEOUT))
        except OutOfTime:
            break
        except ResearchError as error:
            print(f"    {url}: {error}")
            continue
        final_url = page["url"]
        if not same_site(final_url, base_host) or not is_public_http_url(final_url):
            continue  # redirected off the business's site, or to a non-public address
        if page_identity(final_url) in read:
            continue
        read.add(page_identity(final_url))
        text = clean_markdown(page["content"])
        title = page["title"].casefold()
        head = text[:300].casefold()
        if (page["status"] or 200) >= 400 or len(text.strip()) < MIN_RENDERED_TEXT_CHARS or any(
            marker in title or marker in head for marker in ("page not found", "404", "not found")
        ):
            continue
        rendered += 1
        hrefs = [href for _, href in page["links"]]
        mail_links = [href[7:] for href in hrefs if href.casefold().startswith("mailto:")]
        people = extract_people(text, roles, evidence.business_name)
        evidence.add_people(people, "website", final_url)
        evidence.add_emails(EMAIL_PATTERN.findall(text) + [m.split("?")[0] for m in mail_links], "website", final_url)
        evidence.add_linkedin(hrefs)
        print(f"    {final_url}: {len(people)} person(s)")
    return rendered


def build_queries(business: dict) -> list[str]:
    location = business_location(business)
    place = f", {location}" if location else ""
    name = search_name(business["name"])
    return [
        f"Who is owner/stakeholders of {name}{place}?",
        f'"{name}" owner',
        f'"{name}" "office manager"',
    ]


def run_search(query: str, evidence: Evidence, roles: RoleConfig, deadline: float | None = None) -> bool:
    """One web search through SearXNG; each result's title and snippet is read on its own (a person counts only when
    the snippet mentions the business). Returns whether the search answered."""
    try:
        results = searxng.search(query, timeout=call_timeout(deadline, searxng.TIMEOUT))
    except searxng.SearchUnavailable as error:
        print(f"  Search failed: {error}")
        return False
    echo = ascii_token(query.replace('"', ""))
    found = 0
    for result in results[:MAX_SEARCH_RESULTS]:
        text = f"{result['title']}\n{result['content']}"
        text = "\n".join(line for line in text.splitlines() if echo not in ascii_token(line))  # the query echoed back
        people = extract_people(text, roles, evidence.business_name)
        evidence.add_people(people, "search_snippet", result["url"], require_mention=True)
        evidence.add_emails(EMAIL_PATTERN.findall(text), "search", result["url"])
        evidence.add_linkedin([result["url"]])
        found += len(people)
    if not found:
        print(f"  No target-role people in {len(results)} result(s).")
    return True


# ---------------------------------------------------------------- database


def load_businesses(min_score: int | None, limit: int | None, business_ids: list[int] | None, redo: bool) -> list[dict]:
    queryset = (
        Business.objects.filter(qualify.qualified(min_score), name__isnull=False)
        .order_by(F("website_profile__qualification_score").desc(nulls_last=True), "id")
    )
    if not redo:
        queryset = queryset.filter(Q(website_profile__isnull=True) | Q(website_profile__contact_discovery_status__isnull=True))
    if business_ids:
        queryset = queryset.filter(id__in=business_ids)
    if limit is not None:
        queryset = queryset[:limit]
    return list(
        queryset.values(
            "id", "name", "city", "address", "country", "phone", "website_url", "domain",
            site_emails=F("website_profile__emails"),
            scraped_urls=F("website_profile__scraped_urls"),
            scraped_pages=F("website_profile__scraped_pages"),
            discovered_links=F("website_profile__discovered_links"),
            qualification_score=F("website_profile__qualification_score"),
        )
    )


def save_contacts(business_id: int, rows: list[dict]) -> None:
    """Replace the contacts this module found earlier (manual ones are kept) and mark discovery done."""
    status = "completed" if rows else "no_contacts"
    with transaction.atomic():
        BusinessContact.objects.filter(business_id=business_id).filter(
            Q(email_source__in=OWN_EMAIL_SOURCES) | Q(email_source__isnull=True)
        ).delete()
        BusinessContact.objects.bulk_create([BusinessContact(**row) for row in rows])
        BusinessWebsiteProfile.objects.update_or_create(  # an approved business without a website has no profile yet
            business_id=business_id,
            defaults={"contact_discovery_status": status, "contact_discovery_at": datetime.now(timezone.utc)},
        )


def collect_site_evidence(business: dict, evidence: Evidence, roles: RoleConfig) -> None:
    """Waterfall steps 1-2: mine the Module 2 profile (pages, findings, emails, links)."""
    evidence.add_emails(business["site_emails"] or [], "website", business["website_url"] or "")
    evidence.add_linkedin(link.get("url", "") for link in business["discovered_links"] or [])
    for page in business["scraped_pages"] or []:
        url = page.get("url") or business["website_url"] or ""
        notes = "\n".join([page.get("summary") or "", *(page.get("relevant_findings") or [])])
        for text in (page.get("content") or "", notes):
            evidence.add_people(extract_people(text, roles, business["name"]), "website", url)
        evidence.add_emails(page.get("emails") or [], "website", url)
        evidence.add_linkedin(link.get("url", "") for link in page.get("links") or [])


def add_arguments(parser) -> None:
    parser.add_argument("--min-score", type=int,
                        help="Minimum qualification score (default: each campaign's minimum score). Businesses a "
                             "person approved are always included, rejected ones never.")
    parser.add_argument("--limit", type=int, help="Maximum number of businesses in this run.")
    parser.add_argument("--business-id", type=int, action="append", help="Process this business only (repeatable).")
    parser.add_argument("--redo", action="store_true", help="Reprocess businesses that were already processed.")
    parser.add_argument("--roles-file", type=Path, default=DEFAULT_ROLES_FILE, help="Target-role configuration JSON.")
    parser.add_argument("--max-searches", type=int, default=3, help="Maximum searches per business (default: 3).")
    parser.add_argument("--no-search", action="store_true", help="Skip web search (SearXNG).")
    parser.add_argument("--no-crawl", action="store_true", help="Skip reading the business's own site pages.")
    parser.add_argument("--max-pages", type=int, default=6, help="Maximum site pages to read per business (default: 6).")
    parser.add_argument("--dry-run", action="store_true", help="Print the contacts without writing to the database.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between searches (default: 1).")


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    if options["min_score"] is not None and not 0 <= options["min_score"] <= 100:
        raise CommandError("--min-score must be between 0 and 100")
    if options["max_searches"] < 0 or options["delay"] < 0 or options["max_pages"] < 0:
        raise CommandError("--max-searches, --max-pages and --delay cannot be negative")
    try:
        roles = load_role_config(options["roles_file"])
    except (OSError, ValueError, KeyError) as error:
        raise CommandError(f"Cannot read the roles file {options['roles_file']}: {error}") from error

    search = not options["no_search"] and options["max_searches"] > 0
    if search and not searxng.configured():
        print("SEARXNG_URL is not set, so there is no web search; only the website is used.")
        search = False
    reader = JinaReader(max_wait=20 if options.get("deadline") is not None else None)
    try:
        queue = load_businesses(options["min_score"], options["limit"], options["business_id"], options["redo"])
        if not queue:
            print("No qualified businesses need contact discovery (use --redo to reprocess finished ones).")
            return 0

        threshold = options["min_score"] if options["min_score"] is not None else "the campaign's minimum"
        print(f"Discovering contacts for {len(queue)} businesses (score >= {threshold}, or approved by a person).")
        stored = empty = deferred = searches_used = 0
        for position, business in enumerate(queue):
            if out_of_time(options, BUSINESS_RESERVE_SECONDS):
                time_limit_note(len(queue) - position, "business(es)")
                break
            domains = collect_domains(business)
            evidence = Evidence(search_name(business["name"]), domains, context_tokens_for(business, domains),
                                phone_key(business["phone"]))
            print(f"Business {business['id']}: {business['name']}", flush=True)
            collect_site_evidence(business, evidence, roles)
            print(f"  Module 2 data: {len(evidence.people)} person(s), target role found: {evidence.has_target()}.")

            if not evidence.has_target() and not options["no_crawl"] and options["max_pages"]:
                print("  Reading site pages...")
                pages = crawl_site(reader, business, evidence, roles, options["max_pages"], options)
                print(f"  Read {pages} page(s); target role found: {evidence.has_target()}.")

            search_ok = True
            if not evidence.has_target() and search:
                search_ok = False
                for query in build_queries(business)[: options["max_searches"]]:
                    if out_of_time(options, SEARCH_RESERVE_SECONDS):
                        break
                    if searches_used:
                        time.sleep(options["delay"])
                    searches_used += 1
                    print(f"  Search: {query}")
                    try:
                        search_ok = run_search(query, evidence, roles, options.get("deadline")) or search_ok
                    except OutOfTime:
                        break
                    if evidence.has_target():
                        break

            rows = build_contacts(business["id"], evidence)
            if not rows and not search_ok:
                deferred += 1
                print("  Search did not answer; leaving this business for a later run.")
                continue
            for row in rows:
                marker = "*" if row["is_primary"] else " "
                print(
                    f"  {marker} {row['name']} | {row['job_title']} | {row['email']} ({row['email_source']}) "
                    f"| conf {row['confidence']}"
                )
            if not rows:
                print("  No decision maker found.")
            if options["dry_run"]:
                continue
            save_contacts(business["id"], rows)
            if rows:
                stored += 1
            else:
                empty += 1
        print(f"Done: {stored} with contacts, {empty} with none found, {deferred} deferred.")
        return 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `python manage.py migrate` first.")
        return 1
