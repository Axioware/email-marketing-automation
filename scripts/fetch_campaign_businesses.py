import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from sqlalchemy import MetaData, Table, create_engine, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError


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


def update_campaign_status(engine, campaigns: Table, campaign_id: int, status: str) -> None:
    values = {"status": status}
    if status == "running":
        values.update(started_at=func.now(), completed_at=None)
    else:
        values["completed_at"] = func.now()

    with engine.begin() as connection:
        connection.execute(
            update(campaigns).where(campaigns.c.id == campaign_id).values(**values)
        )


def prompt_for_positive_integer(prompt: str) -> int:
    while True:
        try:
            value = int(input(prompt).strip())
            if value > 0:
                return value
        except ValueError:
            pass
        print("Enter a positive whole number.")


def choose_campaign(connection, campaigns: Table):
    rows = connection.execute(
        select(
            campaigns.c.id,
            campaigns.c.name,
            campaigns.c.status,
            campaigns.c.target_country,
            campaigns.c.target_locations,
            campaigns.c.search_terms,
        ).order_by(campaigns.c.created_at.desc())
    ).mappings().all()

    if not rows:
        print("No discovery campaigns found. Create one first.")
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
        search_terms = parse_comma_separated_values(
            input("Campaign has no search terms. Enter terms (comma-separated): ")
        )

    if not locations:
        fallback_location = country or input("No campaign locations. Enter a location: ").strip()
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

    input("Complete Google consent in the browser, then press Enter to continue: ")
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
        name = heading.inner_text(timeout=1500).strip() or card["name"]
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
    domain = urlsplit(website_url).hostname if website_url else None

    return {
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
    }


def existing_business_keys(connection, businesses: Table, campaign_id: int) -> set[tuple[str, str]]:
    rows = connection.execute(
        select(businesses.c.google_place_id, businesses.c.google_maps_url).where(
            businesses.c.discovery_campaign_id == campaign_id
        )
    )
    keys = set()
    for place_id, maps_url in rows:
        if place_id:
            keys.add(("place", place_id))
        if maps_url:
            keys.add(("url", maps_url))
    return keys


def scrape_campaign(
    page,
    campaign,
    queries: list[str],
    businesses: Table,
    engine,
    limit: int,
    delay: float,
    headed: bool,
):
    with engine.connect() as connection:
        seen_keys = existing_business_keys(connection, businesses, campaign["id"])

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
            if ("url", card["url"]) in seen_keys:
                continue

            time.sleep(delay)
            try:
                details = extract_business_details(
                    page, card, campaign["target_country"], headed
                )
            except RuntimeError:
                raise
            except PlaywrightError as error:
                print(f"Skipping a Maps result ({type(error).__name__}).")
                continue

            place_id = details["google_place_id"]
            if place_id and ("place", place_id) in seen_keys:
                continue

            discovered_at = datetime.now(timezone.utc)
            details.update(
                discovery_campaign_id=campaign["id"],
                first_discovered_at=discovered_at,
                last_discovered_at=discovered_at,
            )
            records.append(details)
            if place_id:
                seen_keys.add(("place", place_id))
            seen_keys.add(("url", details["google_maps_url"]))

    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Google Maps businesses for a campaign.")
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
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set in the environment or project .env file.")
        return 1

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    try:
        campaigns = Table("discovery_campaigns", metadata, autoload_with=engine)
        businesses = Table("businesses", metadata, autoload_with=engine)
        with engine.connect() as connection:
            campaign = choose_campaign(connection, campaigns)
        if campaign is None:
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
            update_campaign_status(engine, campaigns, campaign["id"], "running")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not args.headed)
                try:
                    page = browser.new_page()
                    page.set_default_timeout(10000)
                    records = scrape_campaign(
                        page,
                        campaign,
                        queries,
                        businesses,
                        engine,
                        limit,
                        args.delay,
                        args.headed,
                    )
                finally:
                    browser.close()
            if records:
                with engine.begin() as connection:
                    connection.execute(insert(businesses), records)
            update_campaign_status(engine, campaigns, campaign["id"], "completed")
        except Exception as error:
            try:
                update_campaign_status(engine, campaigns, campaign["id"], "failed")
            except SQLAlchemyError:
                print("Could not persist the campaign's failed status.")
            print(f"Campaign {campaign['id']} failed ({type(error).__name__}): {error}")
            if isinstance(error, PlaywrightError):
                print("Install the browser with `python -m playwright install chromium`.")
            return 1
        except BaseException:
            try:
                update_campaign_status(engine, campaigns, campaign["id"], "failed")
            except SQLAlchemyError:
                print("Could not persist the campaign's failed status.")
            raise

        if not records:
            print("No new businesses were found for this campaign.")
            print(f"Campaign {campaign['id']} completed with no new businesses.")
            return 0

        print(f"Stored {len(records)} businesses for campaign {campaign['id']}.")
        return 0
    except SQLAlchemyError as error:
        print(f"Database operation failed ({type(error).__name__}).")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())