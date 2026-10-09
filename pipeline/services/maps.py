"""Module 1: fetch businesses for a discovery campaign from Google Maps (Playwright + Chromium)."""
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import phonenumbers
import pycountry
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone as django_timezone
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from pipeline.models import Business, BusinessSource, DiscoveryCampaign
from pipeline.services.console import interactive, wait_for_person
from pipeline.services.dbthread import outside_event_loop


def normalize_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )
    return " ".join(normalized.split()) or None


def country_region(country: str | None) -> str | None:
    country_name = normalize_text(country)
    if not country_name:
        return None

    aliases = {"UK": "GB", "USA": "US", "U.S.A.": "US"}
    if country_name.upper() in aliases:
        return aliases[country_name.upper()]
    try:
        return pycountry.countries.lookup(country_name).alpha_2
    except LookupError:
        return None


def normalize_phone(value: str | None, country: str | None = None) -> str | None:
    phone = normalize_text(value)
    if not phone:
        return None

    region = None if phone.startswith("+") else country_region(country)
    try:
        parsed = phonenumbers.parse(phone, region)
        if phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass

    digits = "".join(character for character in phone if character.isdecimal())
    if not digits:
        return None
    return f"+{digits}" if phone.startswith("+") else digits


def normalize_website(value: str | None) -> str | None:
    website = normalize_text(value)
    if not website:
        return None
    if website.startswith("//"):
        website = f"https:{website}"
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", website):
        website = f"https://{website}"

    try:
        parsed = urlsplit(website)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if not hostname or parsed.scheme.casefold() not in {"http", "https"}:
        return None

    hostname = hostname.removeprefix("www.")
    if port and not (parsed.scheme.casefold() == "http" and port == 80) and not (
        parsed.scheme.casefold() == "https" and port == 443
    ):
        hostname = f"{hostname}:{port}"

    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), hostname, path, parsed.query, ""))


def normalize_name_key(value: str | None) -> str | None:
    name = normalize_text(value)
    if not name:
        return None
    name = re.sub(r"(?<=[^\W_])['’](?=[^\W_])", "", name)
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return " ".join(re.findall(r"[^\W_]+", decomposed)) or None


def normalize_domain(value: str | None) -> str | None:
    website = normalize_website(value)
    if not website:
        return None
    return (urlsplit(website).hostname or "").removeprefix("www.") or None


def parse_comma_separated_values(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        return []
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


@outside_event_loop
def update_campaign_status(campaign_id: int, status: str) -> None:
    values = {"status": status}
    if status == "running":
        values.update(started_at=django_timezone.now(), completed_at=None)
    else:
        values["completed_at"] = django_timezone.now()
    DiscoveryCampaign.objects.filter(pk=campaign_id).update(**values)


def prompt_for_positive_integer(prompt: str) -> int:
    while True:
        try:
            value = int(input(prompt).strip())
            if value > 0:
                return value
        except ValueError:
            pass
        print("Enter a positive whole number.")


CAMPAIGN_FIELDS = ("id", "name", "status", "target_country", "target_locations", "search_terms")


def choose_campaign(campaign_id: int | None = None):
    if campaign_id is not None:
        campaign = DiscoveryCampaign.objects.filter(pk=campaign_id).values(*CAMPAIGN_FIELDS).first()
        if campaign is None:
            print(f"Discovery campaign {campaign_id} does not exist.")
        return campaign

    rows = list(DiscoveryCampaign.objects.order_by("-created_at").values(*CAMPAIGN_FIELDS))
    if not rows:
        print("No discovery campaigns found. Create one first.")
        return None
    if not interactive():
        print("Pass --campaign-id (no terminal attached to choose a campaign).")
        return None

    print("Discovery campaigns:")
    for campaign in rows:
        country = campaign["target_country"] or "country not set"
        print(
            f"  {campaign['id']}: {campaign['name']} "
            f"[{campaign['status']}] - {country}"
        )

    available_ids = {campaign["id"] for campaign in rows}
    while True:
        value = input("Campaign ID to use: ").strip()
        try:
            campaign_id = int(value)
        except ValueError:
            campaign_id = None
        if campaign_id in available_ids:
            return next(row for row in rows if row["id"] == campaign_id)
        print("Enter an ID from the campaign list.")


def build_search_queries(campaign) -> list[str]:
    search_terms = parse_comma_separated_values(campaign["search_terms"])
    locations = parse_comma_separated_values(campaign["target_locations"])
    country = (campaign["target_country"] or "").strip()

    while not search_terms:
        if not interactive():
            print("The campaign has no search terms; add some to it first.")
            return []
        search_terms = parse_comma_separated_values(
            input("Campaign has no search terms. Enter terms (comma-separated): ")
        )

    if not locations:
        fallback_location = country or (input("No campaign locations. Enter a location: ").strip() if interactive() else "")
        if not fallback_location:
            print("A location is required to search Google Maps.")
            return []
        locations = [fallback_location]

    queries = []
    for term in search_terms:
        for location in locations:
            full_location = location
            if country and country.casefold() not in location.casefold():
                full_location = f"{location}, {country}"
            queries.append(f"{term} in {full_location}")
    return queries


def wait_for_manual_consent(page, headed: bool) -> None:
    if "consent.google.com" not in page.url:
        return
    if not headed:
        raise RuntimeError("Google requires consent. Rerun with --headed to complete it manually.")

    wait_for_person(
        "Complete Google consent in the browser, then press Enter to continue: ",
        lambda: "consent.google.com" not in page.url,
    )
    if "consent.google.com" in page.url:
        raise RuntimeError("Google consent was not completed; scraping stopped.")


def collect_result_cards(
    page, query: str, requested: int, headed: bool
) -> list[dict[str, str | None]]:
    search_url = "https://www.google.com/maps/search/" + quote(query, safe="")
    page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

    wait_for_manual_consent(page, headed)

    feed = page.locator('div[role="feed"]').first
    try:
        feed.wait_for(state="visible", timeout=20000)
    except PlaywrightTimeoutError:
        if page.locator('iframe[src*="recaptcha"], #captcha').count():
            raise RuntimeError("Google presented a CAPTCHA; scraping stopped.")
        return []

    cards_by_url: dict[str, dict[str, str | None]] = {}
    no_growth_count = 0
    max_scrolls = min(max(requested * 2, 8), 100)

    for _ in range(max_scrolls):
        cards = page.locator('a[href*="/maps/place/"]').evaluate_all(
            """anchors => anchors.map(anchor => ({
                url: anchor.href,
                name: anchor.getAttribute('aria-label') || anchor.innerText || null
            })).filter(card => card.url)"""
        )
        for card in cards:
            card["name"] = (card["name"] or "").strip() or None
            cards_by_url.setdefault(card["url"], card)
            if len(cards_by_url) >= requested:
                break

        if len(cards_by_url) >= requested:
            break

        previous_count = len(cards_by_url)
        feed.evaluate("element => element.scrollTo(0, element.scrollHeight)")
        try:
            page.wait_for_function(
                "previous => document.querySelectorAll('a[href*=\"/maps/place/\"]').length > previous",
                arg=previous_count,
                timeout=2500,
            )
        except PlaywrightTimeoutError:
            no_growth_count += 1
            if no_growth_count >= 2:
                break
        else:
            no_growth_count = 0

    return list(cards_by_url.values())[:requested]


def locator_value(page, selector: str) -> str | None:
    locator = page.locator(selector).first
    try:
        if locator.count() == 0:
            return None
        value = locator.get_attribute("aria-label") or locator.inner_text(timeout=1500)
        value = value.strip()
        if ":" in value:
            value = value.split(":", 1)[1].strip()
        return value or None
    except PlaywrightError:
        return None


def extract_business_details(
    page, card: dict[str, str | None], target_country: str | None, headed: bool
):
    page.goto(card["url"], wait_until="domcontentloaded", timeout=60000)
    wait_for_manual_consent(page, headed)

    heading = page.locator("h1").first
    try:
        heading.wait_for(state="visible", timeout=12000)
        name = heading.inner_text(timeout=1500) or card["name"]
    except PlaywrightError:
        name = card["name"]

    address = locator_value(page, 'button[data-item-id="address"]')
    phone = locator_value(page, 'button[data-item-id^="phone:tel:"]')
    category = locator_value(page, 'button[jsaction*="pane.rating.category"]')

    website_locator = page.locator('a[data-item-id="authority"]').first
    try:
        website_url = website_locator.get_attribute("href") if website_locator.count() else None
    except PlaywrightError:
        website_url = None

    rating_text = locator_value(page, "div.F7nice") or ""
    rating_match = re.search(r"(?<!\d)([0-5](?:\.\d)?)", rating_text)
    review_match = re.search(r"\(([\d,.]+)\)", rating_text)
    if not review_match:
        review_match = re.search(r"([\d,]+)\s+reviews?", rating_text, re.IGNORECASE)

    google_rating = float(rating_match.group(1)) if rating_match else None
    google_review_count = (
        int(review_match.group(1).replace(",", "").replace(".", ""))
        if review_match
        else None
    )

    try:
        opening_hours = page.locator("table.eK4R0e").first.evaluate(
            "table => Array.from(table.rows, row => row.innerText.trim()).filter(Boolean)",
            timeout=1500,
        )
        if not opening_hours:
            opening_hours = None
    except PlaywrightError:
        opening_hours = None

    place_id_match = re.search(r"\bChIJ[A-Za-z0-9_-]+", page.url)
    coordinates_match = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", page.url)
    raw_data = {
        "search_result": card,
        "name": name,
        "category": category,
        "website_url": website_url,
        "phone": phone,
        "address": address,
        "rating": rating_text,
        "opening_hours": opening_hours,
        "google_maps_url": page.url,
        "google_place_id": place_id_match.group(0) if place_id_match else None,
        "coordinates": coordinates_match.groups() if coordinates_match else None,
    }
    name = normalize_text(name)
    category = normalize_text(category)
    website_url = normalize_website(website_url)
    domain = urlsplit(website_url).hostname if website_url else None
    phone = normalize_phone(phone, target_country)
    address = normalize_text(address)
    target_country = normalize_text(target_country)
    if opening_hours:
        opening_hours = [normalize_text(hour) for hour in opening_hours]
        opening_hours = [hour for hour in opening_hours if hour]
        if not opening_hours:
            opening_hours = None

    return {
        "business": {
            "name": name[:255] if name else None,
            "category": category,
            "website_url": website_url,
            "domain": domain.removeprefix("www.") if domain else None,
            "phone": phone,
            "address": address,
            "country": target_country,
            "latitude": float(coordinates_match.group(1)) if coordinates_match else None,
            "longitude": float(coordinates_match.group(2)) if coordinates_match else None,
            "google_place_id": place_id_match.group(0) if place_id_match else None,
            "google_maps_url": page.url,
            "google_rating": google_rating,
            "google_review_count": google_review_count,
            "opening_hours": opening_hours,
            "source": "google_maps",
            "source_url": page.url,
        },
        "raw_data": raw_data,
    }


def business_identity(business: dict) -> dict[str, str | None]:
    return {
        "place_id": normalize_text(business.get("google_place_id")),
        "maps_url": normalize_text(business.get("google_maps_url")),
        "name": normalize_name_key(business.get("name")),
        "address": normalize_name_key(business.get("address")),
        "domain": normalize_domain(business.get("domain") or business.get("website_url")),
    }


def same_business(first: dict, second: dict) -> bool:
    first_identity = business_identity(first)
    second_identity = business_identity(second)

    first_place_id = first_identity["place_id"]
    second_place_id = second_identity["place_id"]
    if first_place_id and second_place_id:
        return first_place_id == second_place_id

    if first_identity["maps_url"] and first_identity["maps_url"] == second_identity["maps_url"]:
        return True

    if not first_identity["name"] or first_identity["name"] != second_identity["name"]:
        return False

    first_address = first_identity["address"]
    second_address = second_identity["address"]
    if first_address and second_address:
        return first_address == second_address

    first_domain = first_identity["domain"]
    second_domain = second_identity["domain"]
    return bool(first_domain and first_domain == second_domain)


@outside_event_loop
def existing_businesses(campaign_id: int) -> list[dict]:
    return list(
        Business.objects.filter(discovery_campaign_id=campaign_id).values(
            "google_place_id", "google_maps_url", "name", "website_url", "domain", "address"
        )
    )


def scrape_campaign(
    page,
    campaign,
    queries: list[str],
    limit: int,
    delay: float,
    headed: bool,
):
    seen_businesses = existing_businesses(campaign["id"])

    records = []
    for query in queries:
        if len(records) >= limit:
            break
        print(f"Searching Google Maps: {query}")
        time.sleep(delay)
        cards = collect_result_cards(page, query, limit - len(records), headed)

        for card in cards:
            if len(records) >= limit:
                break
            time.sleep(delay)
            try:
                extracted = extract_business_details(
                    page, card, campaign["target_country"], headed
                )
            except RuntimeError:
                raise
            except PlaywrightError as error:
                print(f"Skipping a Maps result ({type(error).__name__}).")
                continue

            details = extracted["business"]
            if any(same_business(details, seen) for seen in seen_businesses):
                continue

            discovered_at = datetime.now(timezone.utc)
            details.update(
                discovery_campaign_id=campaign["id"],
                first_discovered_at=discovered_at,
                last_discovered_at=discovered_at,
            )
            records.append(extracted)
            seen_businesses.append(details)

    return records


@outside_event_loop
def save_records(records: list[dict]) -> None:
    with transaction.atomic():
        for record in records:
            business = Business.objects.create(**record["business"])
            BusinessSource.objects.create(
                business=business,
                source="google_maps",
                source_business_id=business.google_place_id,
                source_url=business.google_maps_url,
                raw_data=record["raw_data"],
                discovered_at=business.first_discovered_at,
            )


def add_arguments(parser) -> None:
    parser.add_argument("--campaign-id", type=int, help="Campaign to fetch for (asked interactively when omitted).")
    parser.add_argument("--limit", type=int, help="How many businesses to fetch (asked interactively when omitted).")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show Chromium (useful when Google requires manual consent).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds to wait between Google Maps page visits (default: 2).",
    )


def run(options: dict) -> int:
    if options["delay"] < 0:
        raise CommandError("--delay cannot be negative")
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")

    try:
        campaign = choose_campaign(options["campaign_id"])
        if campaign is None:
            return 1

        limit = options["limit"]
        if limit is None:
            if not interactive():
                print("Pass --limit (no terminal attached to ask how many businesses to fetch).")
                return 1
            limit = prompt_for_positive_integer("How many businesses to fetch? ")
        queries = build_search_queries(campaign)
        if not queries:
            return 1

        print(
            "Automated Google Maps access may be restricted by Google's terms. "
            "For production use, prefer the official Places API."
        )

        records = []
        try:
            update_campaign_status(campaign["id"], "running")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not options["headed"])
                try:
                    page = browser.new_page()
                    page.set_default_timeout(10000)
                    records = scrape_campaign(
                        page,
                        campaign,
                        queries,
                        limit,
                        options["delay"],
                        options["headed"],
                    )
                finally:
                    browser.close()
            if records:
                save_records(records)
            update_campaign_status(campaign["id"], "completed")
        except Exception as error:
            try:
                update_campaign_status(campaign["id"], "failed")
            except DatabaseError:
                print("Could not persist the campaign's failed status.")
            print(f"Campaign {campaign['id']} failed ({type(error).__name__}): {error}")
            if isinstance(error, PlaywrightError):
                print("Install the browser with `python -m playwright install chromium`.")
            return 1
        except BaseException:
            try:
                update_campaign_status(campaign["id"], "failed")
            except DatabaseError:
                print("Could not persist the campaign's failed status.")
            raise

        if not records:
            print("No new businesses were found for this campaign.")
            print(f"Campaign {campaign['id']} completed with no new businesses.")
            return 0

        print(f"Stored {len(records)} businesses for campaign {campaign['id']}.")
        return 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}).")
        print("Check DATABASE_URL and run `python manage.py migrate` first.")
        return 1
