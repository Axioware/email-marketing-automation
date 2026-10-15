import argparse
import json
import os
import re
import shutil
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from sqlalchemy import MetaData, Table, create_engine, delete, insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

DEFAULT_ROLES_FILE = Path(__file__).resolve().parents[1] / "config" / "target_roles_dental.json"
MAX_PAGE_TEXT_CHARS = 8000
MAX_CONTACTS_PER_BUSINESS = 4
MAX_FALLBACK_CONTACTS = 2
MAX_DOMAINS = 2
MAX_TITLE_CHARS = 80
AI_OVERVIEW_SETTLE_SECONDS = 3
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
AI_OVERVIEW_END_MARKERS = (
    "show more", "dive deeper", "ai responses may include mistakes", "generative ai is experimental",
    "people also ask", "sources", "feedback",
)
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
RENDER_SETTLE_SECONDS = 2
CRAWL_PAGE_DELAY_SECONDS = 1.0
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class StakeholderError(RuntimeError):
    pass


class SearchBlocked(StakeholderError):
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


# ---------------------------------------------------------------- browser and search


def start_firefox(headed: bool, profile: str | None):
    """Launch the locally installed Firefox through geckodriver."""
    if Path("/snap/bin/firefox").exists():
        # Snap Firefox cannot read the default /tmp, where Selenium stores temporary profiles.
        tmpdir = Path.home() / "snap" / "firefox" / "common" / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(tmpdir)
    options = Options()
    if not headed:
        options.add_argument("-headless")
    options.page_load_strategy = "eager"
    options.set_preference("intl.accept_languages", "en-US, en")
    if profile:
        options.profile = FirefoxProfile(profile)  # works on a copy, so your real profile is untouched
    geckodriver = shutil.which("geckodriver")
    service = Service(executable_path=geckodriver) if geckodriver else Service()
    driver = webdriver.Firefox(options=options, service=service)
    driver.set_page_load_timeout(60)
    return driver


def has_element(driver, selector: str) -> bool:
    return bool(driver.find_elements(By.CSS_SELECTOR, selector))


def wait_for_element(driver, selector: str, error_message: str) -> None:
    try:
        WebDriverWait(driver, 15).until(lambda d: has_element(d, selector))
    except TimeoutException:
        raise StakeholderError(error_message)


def element_text(driver, selector: str) -> str:
    for element in driver.find_elements(By.CSS_SELECTOR, selector):
        text = element.text
        if text.strip():
            return text
    return ""


def linkedin_hrefs(driver) -> list[str]:
    hrefs = []
    for element in driver.find_elements(By.CSS_SELECTOR, "a[href*='linkedin.com/in/']"):
        try:
            hrefs.append(element.get_attribute("href") or "")
        except WebDriverException:
            continue
    return hrefs


def wait_for_manual_consent(driver, headed: bool) -> None:
    if "consent." not in driver.current_url.split("?")[0]:
        return
    if not headed:
        raise SearchBlocked("Search engine requires consent. Rerun with --headed to complete it manually.")
    input("Complete the consent prompt in the browser, then press Enter to continue: ")


def extract_ai_overview(page_text: str) -> str | None:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.casefold() == "ai overview":
            section = []
            for follow in lines[index + 1:]:
                if follow.casefold().startswith(AI_OVERVIEW_END_MARKERS):
                    break
                section.append(follow)
            return "\n".join(section)[:MAX_PAGE_TEXT_CHARS] or None
    return None


def search_google(driver, query: str, headed: bool) -> tuple[str, str | None, list[str]]:
    """Return (search_url, AI Overview text or None, linkedin links on the page)."""
    search_url = f"https://www.google.com/search?hl=en&q={quote(query, safe='')}"
    driver.get(search_url)
    wait_for_manual_consent(driver, headed)
    if "/sorry/" in driver.current_url or has_element(driver, 'iframe[src*="recaptcha"], #captcha-form'):
        raise SearchBlocked("Google presented a CAPTCHA.")
    wait_for_element(driver, "#search, #rso", "Google results did not load.")
    time.sleep(AI_OVERVIEW_SETTLE_SECONDS)  # the AI Overview renders after the main results
    return search_url, extract_ai_overview(element_text(driver, "#rso, #center_col, #search")), linkedin_hrefs(driver)


def search_duckduckgo(driver, query: str, headed: bool) -> tuple[str, str, list[str]]:
    """Return (search_url, results text including any AI-assisted answer, linkedin links)."""
    search_url = f"https://duckduckgo.com/?q={quote(query, safe='')}&ia=web&kl=us-en"
    driver.get(search_url)
    wait_for_manual_consent(driver, headed)
    if has_element(driver, ".anomaly-modal__modal, form[action*='anomaly']"):
        raise SearchBlocked("DuckDuckGo presented a bot challenge.")
    wait_for_element(driver, '[data-testid="result"], article', "DuckDuckGo results did not load.")
    time.sleep(AI_OVERVIEW_SETTLE_SECONDS)
    text = "\n".join(line.strip() for line in element_text(driver, "#react-layout, body").splitlines() if line.strip())
    return search_url, text[:MAX_PAGE_TEXT_CHARS], linkedin_hrefs(driver)


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


def crawl_site(driver, business: dict, evidence: Evidence, roles: RoleConfig, max_pages: int) -> int:
    """Waterfall step 2: render the site's team/about/contact pages in Firefox and read what a visitor sees."""
    base_host = host_of(business.get("website_url")) or ""
    rendered = 0
    for url in choose_site_pages(business, max_pages):
        if evidence.has_target():
            break
        try:
            driver.get(url)
            if not same_site(driver.current_url, base_host):
                continue  # redirected off the business's site
            time.sleep(RENDER_SETTLE_SECONDS)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            text = driver.find_element(By.TAG_NAME, "body").text
            title = (driver.title or "").casefold()
            final_url = driver.current_url
            mail_links = [
                (el.get_attribute("href") or "")[7:] for el in driver.find_elements(By.CSS_SELECTOR, "a[href^='mailto:']")
            ]
            linkedin_links = linkedin_hrefs(driver)
        except (TimeoutException, WebDriverException) as error:
            print(f"    {url}: {type(error).__name__}: {str(error).splitlines()[0][:120]}")
            continue
        head = text[:300].casefold()
        if len(text.strip()) < MIN_RENDERED_TEXT_CHARS or any(
            marker in title or marker in head for marker in ("page not found", "404", "not found")
        ):
            continue
        rendered += 1
        people = extract_people(text, roles, evidence.business_name)
        evidence.add_people(people, "website", final_url)
        evidence.add_emails(EMAIL_PATTERN.findall(text) + [m.split("?")[0] for m in mail_links], "website", final_url)
        evidence.add_linkedin(linkedin_links)
        print(f"    {final_url}: {len(people)} person(s)")
        time.sleep(CRAWL_PAGE_DELAY_SECONDS)
    return rendered


def build_queries(business: dict) -> list[str]:
    location = business_location(business)
    place = f", {location}" if location else ""
    name = business["name"]
    return [
        f"Who is owner/stakeholders of {name}{place}?",
        f'"{name}" owner',
        f'"{name}" "office manager"',
    ]


def run_search(driver, query: str, headed: bool, blocked: dict, evidence: Evidence, roles: RoleConfig) -> bool:
    """Google AI Overview first, DuckDuckGo as fallback. Returns True if any engine answered."""
    answered = False
    for engine_name in ("google", "duckduckgo"):
        if blocked[engine_name]:
            continue
        try:
            if engine_name == "google":
                url, text, links = search_google(driver, query, headed)
                answered = True
                if text is None:
                    print("  Google showed no AI Overview; falling back to DuckDuckGo.")
                    continue
                if not mentions_business(text, evidence.business_name, evidence.context_tokens, evidence.phone):
                    print("  AI Overview does not mention the business; ignoring it.")
                    continue
                source = "search_ai"
            else:
                url, text, links = search_duckduckgo(driver, query, headed)
                answered = True
                source = "search_snippet"
            echo = ascii_token(query.replace('"', ""))
            text = "\n".join(line for line in text.splitlines() if echo not in ascii_token(line))  # query echoed by the page
            people = extract_people(text, roles, evidence.business_name)
            evidence.add_people(people, source, url, require_mention=(source == "search_snippet"))
            evidence.add_emails(EMAIL_PATTERN.findall(text), "search", url)
            evidence.add_linkedin(links)
            if people:
                return True
            print(f"  No target-role people in {engine_name} results.")
        except SearchBlocked as error:
            blocked[engine_name] = True
            print(f"  {error} Skipping {engine_name} for the rest of this run.")
        except (StakeholderError, WebDriverException) as error:
            print(f"  {engine_name} failed: {str(error).splitlines()[0]}")
    return answered


# ---------------------------------------------------------------- database


def load_businesses(connection, businesses, profiles, min_score, limit, business_id, redo):
    statement = (
        select(
            businesses.c.id,
            businesses.c.name,
            businesses.c.city,
            businesses.c.address,
            businesses.c.country,
            businesses.c.phone,
            businesses.c.website_url,
            businesses.c.domain,
            profiles.c.emails.label("site_emails"),
            profiles.c.scraped_urls,
            profiles.c.scraped_pages,
            profiles.c.discovered_links,
            profiles.c.qualification_score,
        )
        .join(profiles, profiles.c.business_id == businesses.c.id)
        .where(
            profiles.c.status == "completed",
            profiles.c.qualification_score >= min_score,
            businesses.c.name.is_not(None),
        )
        .order_by(profiles.c.qualification_score.desc(), businesses.c.id)
    )
    if not redo:
        statement = statement.where(profiles.c.contact_discovery_status.is_(None))
    if business_id is not None:
        statement = statement.where(businesses.c.id == business_id)
    if limit is not None:
        statement = statement.limit(limit)
    return [dict(row._mapping) for row in connection.execute(statement)]


def save_contacts(engine, contacts_table, profiles, business_id: int, rows: list[dict]) -> None:
    status = "completed" if rows else "no_contacts"
    with engine.begin() as connection:
        connection.execute(
            delete(contacts_table).where(
                contacts_table.c.business_id == business_id,
                or_(contacts_table.c.email_source.in_(OWN_EMAIL_SOURCES), contacts_table.c.email_source.is_(None)),
            )
        )
        if rows:
            connection.execute(insert(contacts_table), rows)
        connection.execute(
            update(profiles)
            .where(profiles.c.business_id == business_id)
            .values(contact_discovery_status=status, contact_discovery_at=datetime.now(timezone.utc))
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decision-maker and contact discovery: Module 2 data, then web search, then email inference."
    )
    parser.add_argument("--min-score", type=int, default=50, help="Minimum qualification score (default: 50).")
    parser.add_argument("--limit", type=int, help="Maximum number of businesses in this run.")
    parser.add_argument("--business-id", type=int, help="Process one business only.")
    parser.add_argument("--redo", action="store_true", help="Reprocess businesses that were already processed.")
    parser.add_argument("--roles-file", type=Path, default=DEFAULT_ROLES_FILE, help="Target-role configuration JSON.")
    parser.add_argument("--max-searches", type=int, default=3, help="Maximum searches per business (default: 3).")
    parser.add_argument("--no-search", action="store_true", help="Skip web search.")
    parser.add_argument("--no-crawl", action="store_true", help="Skip rendering the business's own site pages.")
    parser.add_argument("--max-pages", type=int, default=6, help="Maximum site pages to render per business (default: 6).")
    parser.add_argument("--dry-run", action="store_true", help="Print the contacts without writing to the database.")
    parser.add_argument("--headed", action="store_true", help="Show Firefox (needed for consent/CAPTCHA).")
    parser.add_argument("--profile", help="Firefox profile folder to copy and use (keeps Google cookies/consent).")
    parser.add_argument("--delay", type=float, default=6.0, help="Seconds between searches (default: 6).")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if not 0 <= args.min_score <= 100:
        parser.error("--min-score must be between 0 and 100")
    if args.max_searches < 0 or args.delay < 0 or args.max_pages < 0:
        parser.error("--max-searches, --max-pages and --delay cannot be negative")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set in the environment or project .env file")
    roles = load_role_config(args.roles_file)

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    driver = None
    try:
        businesses = Table("businesses", metadata, autoload_with=engine)
        profiles = Table("business_website_profiles", metadata, autoload_with=engine)
        contacts_table = Table("business_contacts", metadata, autoload_with=engine)
        with engine.connect() as connection:
            queue = load_businesses(
                connection, businesses, profiles, args.min_score, args.limit, args.business_id, args.redo
            )
        if not queue:
            print("No qualified businesses need contact discovery (use --redo to reprocess finished ones).")
            return 0

        print(f"Discovering contacts for {len(queue)} businesses (score >= {args.min_score}).")
        blocked = {"google": False, "duckduckgo": False}
        stored = empty = deferred = searches_used = 0
        for business in queue:
            domains = collect_domains(business)
            evidence = Evidence(business["name"], domains, context_tokens_for(business, domains), phone_key(business["phone"]))
            print(f"Business {business['id']}: {business['name']}")
            collect_site_evidence(business, evidence, roles)
            print(f"  Module 2 data: {len(evidence.people)} person(s), target role found: {evidence.has_target()}.")

            def ensure_driver():
                nonlocal driver
                if driver is None:
                    driver = start_firefox(args.headed, args.profile)
                return driver

            try:
                if not evidence.has_target() and not args.no_crawl and args.max_pages:
                    print("  Rendering site pages in Firefox...")
                    pages = crawl_site(ensure_driver(), business, evidence, roles, args.max_pages)
                    print(f"  Rendered {pages} page(s); target role found: {evidence.has_target()}.")
            except WebDriverException as error:
                print(f"Could not start Firefox ({type(error).__name__}): {str(error).splitlines()[0]}")
                print("Install Firefox and geckodriver (https://github.com/mozilla/geckodriver/releases).")
                return 1

            search_ok = True
            if not evidence.has_target() and not args.no_search and args.max_searches:
                try:
                    ensure_driver()
                except WebDriverException as error:
                    print(f"Could not start Firefox ({type(error).__name__}): {str(error).splitlines()[0]}")
                    return 1
                search_ok = False
                for query in build_queries(business)[: args.max_searches]:
                    if all(blocked.values()):
                        break
                    if searches_used:
                        time.sleep(args.delay)
                    searches_used += 1
                    print(f"  Search: {query}")
                    search_ok = run_search(driver, query, args.headed, blocked, evidence, roles) or search_ok
                    if evidence.has_target():
                        break

            rows = build_contacts(business["id"], evidence)
            if not rows and not search_ok:
                deferred += 1
                print("  Search was blocked; leaving this business for a later run.")
                continue
            for row in rows:
                marker = "*" if row["is_primary"] else " "
                print(
                    f"  {marker} {row['name']} | {row['job_title']} | {row['email']} ({row['email_source']}) "
                    f"| conf {row['confidence']}"
                )
            if not rows:
                print("  No decision maker found.")
            if args.dry_run:
                continue
            save_contacts(engine, contacts_table, profiles, business["id"], rows)
            if rows:
                stored += 1
            else:
                empty += 1
        print(f"Done: {stored} with contacts, {empty} with none found, {deferred} deferred.")
        return 0
    except SQLAlchemyError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        if driver is not None:
            driver.quit()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
