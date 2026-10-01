import argparse
import os
import re
import shutil
import time
import unicodedata
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
from sqlalchemy import MetaData, Table, create_engine, exists, insert, select
from sqlalchemy.exc import SQLAlchemyError

MAX_PAGE_TEXT_CHARS = 8000
MAX_STAKEHOLDERS = 6
MAX_DOMAINS = 2
AI_OVERVIEW_SETTLE_SECONDS = 3
HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "sir", "mx", "eng"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "dds", "dmd", "phd", "esq", "cpa"}
FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com"}
AI_OVERVIEW_END_MARKERS = (
    "show more",
    "dive deeper",
    "ai responses may include mistakes",
    "generative ai is experimental",
    "people also ask",
    "sources",
    "feedback",
)
NOT_NAME_WORDS = {
    "the", "this", "that", "google", "maps", "overview", "ai", "inc", "llc", "ltd", "company",
    "business", "owner", "owners", "founder", "manager", "director", "partner", "ceo", "president",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "according", "based", "however", "while", "also", "he", "she", "they",
    "co", "managing", "general", "chief", "executive", "officer", "current", "its",
}
BUSINESS_WORDS = {
    "solutions", "dental", "dentistry", "clinic", "clinics", "services", "service", "group", "center",
    "centre", "associates", "studio", "studios", "care", "smile", "smiles", "health", "healthcare",
    "practice", "office", "family", "salon", "spa", "shop", "store", "restaurant", "cafe", "repair",
    "plumbing", "law", "firm", "agency", "consulting", "partners", "enterprises", "holdings", "corp",
    "corporation", "co", "orthodontics", "wellness", "medical", "hospital", "pharmacy", "gym",
}

# Ordered most to least common for small businesses; confidence is for a pure guess.
EMAIL_PATTERNS = [
    ("first", "{first}", 0.30),
    ("first.last", "{first}.{last}", 0.30),
    ("flast", "{f}{last}", 0.20),
    ("firstlast", "{first}{last}", 0.15),
    ("f.last", "{f}.{last}", 0.10),
    ("first_last", "{first}_{last}", 0.10),
    ("last", "{last}", 0.05),
]
INFERRED_PATTERN_CONFIDENCE = 0.60
OBSERVED_EMAIL_CONFIDENCE = 0.90
SECONDARY_DOMAIN_FACTOR = 0.5

NAME = (
    r"(?:(?:Dr|Mr|Mrs|Ms|Prof)\.?\s+)?"
    r"[A-Z][A-Za-z'\u2019\-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'\u2019\-]+)){1,3}"
)
ROLE = (
    r"(?i:co-?owner|owner|co-?founder|founder|managing\s+partner|partner|chief\s+executive(?:\s+officer)?"
    r"|ceo|president|managing\s+director|director|general\s+manager|manager|principal|proprietor)"
)
NAME_LIST = rf"{NAME}(?:\s*(?:,\s*and|,|\band|&)\s*{NAME})*"
# "John Smith is the owner", "John Smith, founder and CEO"
NAME_THEN_ROLE = re.compile(
    rf"(?P<names>{NAME})(?:\s*\([^)]*\))?\s*(?:,|\bis\b|\bare\b|\bwas\b|\bserves as\b|\bas\b|\u2013|-)"
    rf"\s*(?i:the\s+|a\s+|an\s+|its\s+|current\s+)?(?:[A-Za-z\-]+\s+){{0,2}}?(?P<role>{ROLE})\b"
)
# "The owner is John Smith and Jane Doe", "owned by John Smith", "founded by ..."
ROLE_THEN_NAME = re.compile(
    rf"(?:(?P<role>{ROLE})s?\s*(?:\bis\b|\bare\b|\bwas\b|:)\s*"
    rf"|(?P<verb>(?i:owned|founded|co-founded|run|operated|managed|led))\s+(?i:by)\s+)"
    rf"(?P<names>{NAME_LIST})"
)
VERB_ROLES = {
    "owned": "owner", "founded": "founder", "co-founded": "founder", "run": "owner",
    "operated": "owner", "managed": "manager", "led": "director",
}
ROLE_PRIORITY = ["owner", "co_owner", "founder", "partner", "ceo", "director", "manager", "other"]


class StakeholderError(RuntimeError):
    pass


class SearchBlocked(StakeholderError):
    pass


def ascii_token(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", folded.casefold())


def split_name(full_name: str) -> tuple[str | None, str | None]:
    tokens = [token for token in re.split(r"\s+", full_name.replace(",", " ").strip()) if token]
    while tokens and tokens[0].rstrip(".").casefold() in HONORIFICS:
        tokens.pop(0)
    while tokens and tokens[-1].rstrip(".").casefold() in NAME_SUFFIXES:
        tokens.pop()
    if len(tokens) < 2:
        return (tokens[0] if tokens else None), None
    return tokens[0], tokens[-1]


def host_of(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    parsed = urlsplit(value if "//" in value else f"//{value}")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    return host if "." in host and host not in FREE_EMAIL_DOMAINS else None


def collect_domains(business: dict) -> list[str]:
    """Domains from businesses, then the email domains and scraped URLs in website profiles."""
    sources = [business.get("domain"), business.get("website_url")]
    sources += [address.rpartition("@")[2] for address in business.get("site_emails") or []]
    sources += business.get("scraped_urls") or []
    domains = []
    for value in sources:
        host = host_of(value)
        if host and host not in domains:
            domains.append(host)
    return domains[:MAX_DOMAINS]


def candidate_emails(first_name: str | None, last_name: str | None, domain: str | None) -> list[tuple[str, str]]:
    """Return (pattern_name, address) pairs; empty unless both name parts and a domain exist."""
    first = ascii_token(first_name or "")
    last = ascii_token(last_name or "")
    if not first or not last or not domain:
        return []
    values = {"first": first, "last": last, "f": first[0]}
    return [(name, f"{template.format(**values)}@{domain}") for name, template, _ in EMAIL_PATTERNS]


def detect_site_pattern(stakeholders: list[dict], domains: list[str], site_emails: list[str]) -> str | None:
    """If an email already on the site matches a pattern for any stakeholder, trust that pattern."""
    observed = {address.casefold() for address in site_emails}
    for person in stakeholders:
        for domain in domains:
            for pattern_name, address in candidate_emails(person["first_name"], person["last_name"], domain):
                if address in observed:
                    return pattern_name
    return None


def build_contact_rows(
    business_id: int,
    stakeholders: list[dict],
    domains: list[str],
    site_emails: list[str],
    search_url: str,
    source_label: str,
) -> list[dict]:
    observed = {address.casefold() for address in site_emails}
    preferred = detect_site_pattern(stakeholders, domains, site_emails)
    confidence_by_pattern = {name: confidence for name, _, confidence in EMAIL_PATTERNS}
    rows = []
    for index, person in enumerate(stakeholders):
        base = {
            "business_id": business_id,
            "name": person["name"],
            "first_name": person["first_name"],
            "last_name": person["last_name"],
            "job_title": person["job_title"],
            "role_type": person["role_type"],
            "email_status": "unverified",
            "source_urls": [search_url],
            "discovery_reasoning": f"[{source_label}] {person['evidence']}"[:2000],
        }
        candidates = []
        for domain_index, domain in enumerate(domains):
            found = candidate_emails(person["first_name"], person["last_name"], domain)
            if preferred:
                found.sort(key=lambda item: item[0] != preferred)
            candidates += [(pattern_name, address, domain_index) for pattern_name, address in found]
        if not candidates:
            rows.append({**base, "email": None, "email_source": None, "confidence": None, "is_primary": index == 0})
            continue
        for position, (pattern_name, address, domain_index) in enumerate(candidates):
            if address in observed:
                source, confidence = "website", OBSERVED_EMAIL_CONFIDENCE
            else:
                source = "pattern"
                confidence = (
                    INFERRED_PATTERN_CONFIDENCE
                    if preferred and pattern_name == preferred
                    else confidence_by_pattern[pattern_name]
                )
                if domain_index:
                    confidence = round(confidence * SECONDARY_DOMAIN_FACTOR, 3)
            rows.append(
                {
                    **base,
                    "email": address,
                    "email_source": source,
                    "confidence": confidence,
                    "is_primary": index == 0 and position == 0,
                }
            )
    return rows


def role_type_for(role_text: str) -> str:
    role = re.sub(r"[\s-]+", " ", role_text.casefold()).strip()
    if role.startswith("co") and "owner" in role:
        return "co_owner"
    if "owner" in role or "proprietor" in role:
        return "owner"
    if "founder" in role:
        return "founder"
    if "partner" in role:
        return "partner"
    if "ceo" in role or "chief executive" in role or "president" in role:
        return "ceo"
    if "director" in role:
        return "director"
    if "manager" in role:
        return "manager"
    return "other"


def clean_name(raw: str) -> str | None:
    tokens = raw.replace("\u2019", "'").split()
    # "Sonal Shah's Smile Solutions" is a business name, not a person; "Sonal Shah's" alone keeps "Sonal Shah".
    for index, token in enumerate(tokens):
        if token.casefold().endswith("'s"):
            if index < len(tokens) - 1:
                return None
            tokens[index] = token[:-2]
    while tokens and (ascii_token(tokens[0]) in NOT_NAME_WORDS):
        tokens.pop(0)
    while tokens and (ascii_token(tokens[-1]) in NOT_NAME_WORDS):
        tokens.pop()
    while tokens and tokens[0].rstrip(".").casefold() in HONORIFICS:
        tokens.pop(0)
    if len(tokens) < 2:
        return None
    keys = [ascii_token(token) for token in tokens]
    if any(key in NOT_NAME_WORDS or key in BUSINESS_WORDS for key in keys):
        return None
    if not all(keys):
        return None
    return " ".join(tokens)


def sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [pos for pos in (text.find(". ", end), text.find("\n", end)) if pos != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return " ".join(text[left:right].split())[:500]


def extract_stakeholders(text: str, business_name: str) -> list[dict]:
    """Heuristically pull named owners/stakeholders out of search-result text."""
    found = []
    seen = set()

    def add(raw_names: str, role_text: str, start: int, end: int) -> None:
        for raw in re.split(r"\s*(?:,\s*and\s+|,|\band\s+|&)\s*", raw_names):
            name = clean_name(raw)
            key = ascii_token(name) if name else ""
            if not key or key in seen:
                continue
            seen.add(key)
            first_name, last_name = split_name(name)
            found.append(
                {
                    "name": name[:255],
                    "first_name": (first_name or "")[:120] or None,
                    "last_name": (last_name or "")[:120] or None,
                    "job_title": " ".join(role_text.split()).title()[:255],
                    "role_type": role_type_for(role_text),
                    "evidence": sentence_around(text, start, end),
                }
            )

    for match in NAME_THEN_ROLE.finditer(text):
        add(match.group("names"), match.group("role"), match.start(), match.end())
    for match in ROLE_THEN_NAME.finditer(text):
        role_text = match.group("role") or VERB_ROLES[match.group("verb").casefold()]
        add(match.group("names"), role_text, match.start(), match.end())

    found.sort(key=lambda person: ROLE_PRIORITY.index(person["role_type"]))
    return found[:MAX_STAKEHOLDERS]


def start_firefox(headed: bool, profile: str | None):
    """Launch the locally installed Firefox through geckodriver."""
    snap_firefox = Path("/snap/bin/firefox")
    if snap_firefox.exists():
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


def search_google(driver, query: str, headed: bool) -> tuple[str, str | None]:
    """Return (search_url, AI Overview text or None when Google showed no overview)."""
    search_url = f"https://www.google.com/search?hl=en&q={quote(query, safe='')}"
    driver.get(search_url)
    wait_for_manual_consent(driver, headed)
    if "/sorry/" in driver.current_url or has_element(driver, 'iframe[src*="recaptcha"], #captcha-form'):
        raise SearchBlocked("Google presented a CAPTCHA.")
    wait_for_element(driver, "#search, #rso", "Google results did not load.")
    time.sleep(AI_OVERVIEW_SETTLE_SECONDS)  # the AI Overview renders after the main results
    return search_url, extract_ai_overview(element_text(driver, "#rso, #center_col, #search"))


def search_duckduckgo(driver, query: str, headed: bool) -> tuple[str, str]:
    """Return (search_url, results text including any AI-assisted answer)."""
    search_url = f"https://duckduckgo.com/?q={quote(query, safe='')}&ia=web&kl=us-en"
    driver.get(search_url)
    wait_for_manual_consent(driver, headed)
    if has_element(driver, ".anomaly-modal__modal, form[action*='anomaly']"):
        raise SearchBlocked("DuckDuckGo presented a bot challenge.")
    wait_for_element(driver, '[data-testid="result"], article', "DuckDuckGo results did not load.")
    time.sleep(AI_OVERVIEW_SETTLE_SECONDS)
    text = element_text(driver, "#react-layout, body")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return search_url, text[:MAX_PAGE_TEXT_CHARS]


def load_businesses(
    connection,
    businesses: Table,
    profiles: Table,
    contacts: Table,
    min_score: int,
    limit: int | None,
    business_id: int | None,
):
    statement = (
        select(
            businesses.c.id,
            businesses.c.name,
            businesses.c.city,
            businesses.c.website_url,
            businesses.c.domain,
            profiles.c.emails.label("site_emails"),
            profiles.c.scraped_urls,
            profiles.c.qualification_score,
        )
        .join(profiles, profiles.c.business_id == businesses.c.id)
        .where(
            profiles.c.status == "completed",
            profiles.c.qualification_score >= min_score,
            businesses.c.name.is_not(None),
            ~exists().where(contacts.c.business_id == businesses.c.id),
        )
        .order_by(profiles.c.qualification_score.desc(), businesses.c.id)
    )
    if business_id is not None:
        statement = statement.where(businesses.c.id == business_id)
    if limit is not None:
        statement = statement.limit(limit)
    return [dict(row._mapping) for row in connection.execute(statement)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find business owners/stakeholders via Google AI Overview (DuckDuckGo fallback) "
        "and store pattern-guessed emails."
    )
    parser.add_argument("--min-score", type=int, default=50, help="Minimum qualification score (default: 50).")
    parser.add_argument("--limit", type=int, help="Maximum number of businesses in this run.")
    parser.add_argument("--business-id", type=int, help="Process one business only.")
    parser.add_argument("--headed", action="store_true", help="Show Firefox (needed for consent/CAPTCHA).")
    parser.add_argument(
        "--profile",
        help="Path to a Firefox profile folder to copy and use (keeps your Google cookies/consent).",
    )
    parser.add_argument("--delay", type=float, default=6.0, help="Seconds between searches (default: 6).")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if not 0 <= args.min_score <= 100:
        parser.error("--min-score must be between 0 and 100")
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set in the environment or project .env file")

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    try:
        businesses = Table("businesses", metadata, autoload_with=engine)
        profiles = Table("business_website_profiles", metadata, autoload_with=engine)
        contacts = Table("business_contacts", metadata, autoload_with=engine)
        with engine.connect() as connection:
            queue = load_businesses(
                connection, businesses, profiles, contacts, args.min_score, args.limit, args.business_id
            )
        if not queue:
            print("No qualified businesses without contacts were found.")
            return 0

        print("Automated search access may be restricted by Google's and DuckDuckGo's terms.")
        print(f"Finding stakeholders for {len(queue)} businesses (score >= {args.min_score}).")
        stored = empty = failed = 0
        blocked = {"google": False, "duckduckgo": False}
        try:
            driver = start_firefox(args.headed, args.profile)
        except WebDriverException as error:
            print(f"Could not start Firefox ({type(error).__name__}): {str(error).splitlines()[0]}")
            print("Install Firefox and geckodriver (https://github.com/mozilla/geckodriver/releases).")
            return 1
        try:
            for position, business in enumerate(queue):
                if all(blocked.values()):
                    print("Stopping early; both search engines are blocking automated searches.")
                    break
                if position:
                    time.sleep(args.delay)
                place = f", {business['city']}" if business["city"] else ""
                query = f"Who is owner/stakeholders of {business['name']}{place}?"
                print(f"Business {business['id']}: {query}")

                people, search_url, source_label, error_note = [], None, None, None
                for engine_name in ("google", "duckduckgo"):
                    if blocked[engine_name]:
                        continue
                    try:
                        if engine_name == "google":
                            search_url, text = search_google(driver, query, args.headed)
                            if text is None:
                                print("  Google showed no AI Overview; falling back to DuckDuckGo.")
                                continue
                            source_label = "google_ai_overview"
                        else:
                            search_url, text = search_duckduckgo(driver, query, args.headed)
                            source_label = "duckduckgo"
                        people = extract_stakeholders(text, business["name"])
                        if people:
                            break
                        print(f"  No named stakeholders in {engine_name} results.")
                    except SearchBlocked as error:
                        blocked[engine_name] = True
                        error_note = str(error)
                        print(f"  {error} Skipping {engine_name} for the rest of this run.")
                    except (StakeholderError, WebDriverException) as error:
                        error_note = str(error).splitlines()[0]
                        print(f"  {engine_name} failed: {error_note}")

                if not people:
                    if error_note and not search_url:
                        failed += 1
                    else:
                        empty += 1
                    continue
                try:
                    rows = build_contact_rows(
                        business["id"],
                        people,
                        collect_domains(business),
                        business["site_emails"] or [],
                        search_url,
                        source_label,
                    )
                    with engine.begin() as connection:
                        connection.execute(insert(contacts), rows)
                    stored += 1
                    print(f"  Stored {len(people)} stakeholder(s), {len(rows)} contact row(s).")
                except SQLAlchemyError as error:
                    failed += 1
                    print(f"  Database insert failed ({type(error).__name__}).")
        finally:
            driver.quit()
        print(f"Done: {stored} with stakeholders, {empty} with none found, {failed} failed.")
        return 1 if failed else 0
    except SQLAlchemyError as error:
        print(f"Database operation failed ({type(error).__name__}).")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
