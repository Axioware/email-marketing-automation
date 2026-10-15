"""Module 1, second lead source: the Google Maps scraper container (github.com/conor-is-my-name/google-maps-scraper).

The container is a FastAPI service (port 8001 by default; MAPS_SCRAPER_URL in .env, e.g. https://your-vps/scraper
behind nginx) that runs its own headless Chromium. Every request sends `X-API-Key: MAPS_SCRAPER_KEY` (the container's
SCRAPER_API_KEY). `GET /scrape-get?query=...&max_places=...&lang=...&concurrency=...` searches Google Maps, opens every place
and answers with a JSON list once it is done (it gives up after 5 minutes); `GET /` answers when it is up. Each place:
name, place_id, coordinates, address, rating, reviews_count, categories, website, phone (digits with the country code),
hours, link. They are turned into the same records the built-in browser produces, so de-duplication, saving and every
later step work the same whichever source found the business.
"""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from pipeline.services import maps

DEFAULT_URL = "http://127.0.0.1:8001"
SOURCE_NAME = "google_maps_scraper"  # businesses.source / business_sources.source for what the container found
REQUEST_TIMEOUT = 330  # seconds; the container stops a search after 300
MAX_PLACES_PER_SEARCH = 40  # one search must finish inside the container's 5 minutes
DEFAULT_CONCURRENCY = 5  # the container's own default; 1-20, higher is faster but more likely to be blocked
DEFAULT_LANG = "en"
KEY_REFUSED = "refused the key (MAPS_SCRAPER_KEY must equal the container's SCRAPER_API_KEY)"


class ScraperUnavailable(RuntimeError):
    """The container cannot be reached or answered something unusable; the fetch stops."""


def base_url(url: str | None = None) -> str:
    return (url or os.environ.get("MAPS_SCRAPER_URL", "").strip() or DEFAULT_URL).rstrip("/")


def _headers() -> dict:
    key = os.environ.get("MAPS_SCRAPER_KEY", "").strip()
    return {"Accept": "application/json", **({"X-API-Key": key} if key else {})}


def _get(url: str, timeout: float):
    request = Request(url, headers=_headers())
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - a URL from our own settings
        return json.loads(response.read().decode("utf-8") or "null")


def status(url: str | None = None, timeout: float = 3) -> str | None:
    """None when the container answers its health check, else what is wrong."""
    try:
        _get(base_url(url) + "/", timeout)
        return None
    except HTTPError as error:
        return KEY_REFUSED if error.code == 401 else f"answered HTTP {error.code}"
    except (URLError, OSError, ValueError) as error:
        return f"not reachable ({getattr(error, 'reason', error)})"


def search(query: str, max_places: int, url: str | None = None, lang: str = DEFAULT_LANG,
           concurrency: int = DEFAULT_CONCURRENCY) -> list[dict]:
    """The places the container finds for one Google Maps search."""
    params = urlencode({"query": query, "max_places": max_places, "lang": lang, "headless": "true",
                        "concurrency": concurrency})
    try:
        places = _get(f"{base_url(url)}/scrape-get?{params}", REQUEST_TIMEOUT)
    except HTTPError as error:
        if error.code == 504:
            raise TimeoutError(f"the container gave up after 5 minutes; fetch fewer businesses per run "
                               f"(it was asked for {max_places})") from error
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail", "")
        except (ValueError, AttributeError, OSError):
            pass
        if error.code == 401:
            raise ScraperUnavailable(f"the scraper container {KEY_REFUSED}") from error
        raise ScraperUnavailable(f"the scraper container answered HTTP {error.code} {detail}".strip()) from error
    except (URLError, OSError) as error:
        raise ScraperUnavailable(f"the scraper container at {base_url(url)} is not reachable "
                                 f"({getattr(error, 'reason', error)})") from error
    except ValueError as error:
        raise ScraperUnavailable("the scraper container did not answer JSON") from error
    if not isinstance(places, list):
        raise ScraperUnavailable("the scraper container answered something other than a list of places")
    return [place for place in places if isinstance(place, dict)]


def _call(url: str, method: str, timeout: float):
    """A JSON request to the container's job endpoints; errors become ScraperUnavailable (404 stays an HTTPError)."""
    request = Request(url, method=method, headers=_headers(), data=b"" if method == "POST" else None)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - a URL from our own settings
            return json.loads(response.read().decode("utf-8") or "null")
    except HTTPError as error:
        if error.code == 404:
            raise
        if error.code == 401:
            raise ScraperUnavailable(f"the scraper container {KEY_REFUSED}") from error
        raise ScraperUnavailable(f"the scraper container answered HTTP {error.code}") from error
    except (URLError, OSError, ValueError) as error:
        raise ScraperUnavailable(f"the scraper container at {base_url()} is not reachable "
                                 f"({getattr(error, 'reason', error)})") from error


JOB_REQUEST_TIMEOUT = 30


def start_job(query: str, max_places: int, lang: str = DEFAULT_LANG, concurrency: int = DEFAULT_CONCURRENCY) -> str:
    """Start a search in the background on the container (POST /jobs); returns its id at once. For callers with a
    short request limit (Vercel); the container runs one search at a time."""
    params = urlencode({"query": query, "max_places": max_places, "lang": lang, "concurrency": concurrency})
    try:
        answer = _call(f"{base_url()}/jobs?{params}", "POST", JOB_REQUEST_TIMEOUT)
    except HTTPError as error:
        raise ScraperUnavailable("the scraper container has no /jobs endpoint; update it") from error
    if not isinstance(answer, dict) or not answer.get("id"):
        raise ScraperUnavailable("the scraper container did not answer a job id")
    return str(answer["id"])


def job(job_id: str) -> dict | None:
    """The job's state: {"status": queued|running|done|failed, "error", "results" (when done)}; None when the
    container no longer knows it (it restarted)."""
    try:
        answer = _call(f"{base_url()}/jobs/{job_id}", "GET", JOB_REQUEST_TIMEOUT)
    except HTTPError:
        return None
    if not isinstance(answer, dict) or answer.get("status") not in ("queued", "running", "done", "failed"):
        raise ScraperUnavailable("the scraper container answered an unreadable job")
    if answer["status"] == "done":
        answer["results"] = [place for place in answer.get("results") or [] if isinstance(place, dict)]
    return answer


def is_local(url: str | None = None) -> bool:
    return (urlsplit(base_url(url)).hostname or "") in ("127.0.0.1", "localhost", "::1")


def normalize_phone(value, country: str | None) -> str | None:
    """The container gives digits with the country code and no "+" (923001234567); read them as international first."""
    text = maps.normalize_text(str(value)) if value is not None else None
    phonenumbers = maps.phonenumbers
    if text and text.isdecimal() and not text.startswith("0") and phonenumbers is not None:
        try:
            parsed = phonenumbers.parse(f"+{text}", None)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
    return maps.normalize_phone(text, country)


def record_from_place(place: dict, target_country: str | None) -> dict | None:
    """A place from the container as the record the built-in browser produces ({"business": ..., "raw_data": ...})."""
    name = maps.normalize_text(place.get("name"))
    if not name:
        return None
    categories = [maps.normalize_text(item) for item in place.get("categories") or [] if isinstance(item, str)]
    category = next((item for item in categories if item), None)
    website_url = maps.normalize_website(place.get("website"))
    domain = urlsplit(website_url).hostname if website_url else None
    address = maps.normalize_text(place.get("address"))
    country = maps.normalize_text(target_country)
    location = maps.parse_address(address, country)
    coordinates = place.get("coordinates") if isinstance(place.get("coordinates"), dict) else {}
    hours = [maps.normalize_text(item) for item in place.get("hours") or [] if isinstance(item, str)]
    hours = [item for item in hours if item] or None
    rating, reviews = place.get("rating"), place.get("reviews_count")
    maps_url = maps.normalize_text(place.get("link"))

    def number(value, kind):
        try:
            return kind(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "business": {
            "name": name[:255],
            "category": category,
            "website_url": website_url,
            "domain": domain.removeprefix("www.") if domain else None,
            "phone": normalize_phone(place.get("phone"), country),
            "address": address,
            "city": location["city"],
            "state": location["state"],
            "postal_code": location["postal_code"],
            "country": country,
            "latitude": number(coordinates.get("latitude"), float),
            "longitude": number(coordinates.get("longitude"), float),
            "google_place_id": maps.normalize_text(place.get("place_id")) or maps.place_id_from_url(maps_url),
            "google_maps_url": maps_url,
            "google_rating": number(rating, float),
            "google_review_count": number(reviews, int),
            "opening_hours": hours,
            "source": SOURCE_NAME,
            "source_url": maps_url,
        },
        "raw_data": place,
    }


def scrape_campaign(campaign: dict, queries: list[str], limit: int, url: str | None = None, lang: str = DEFAULT_LANG,
                    concurrency: int = DEFAULT_CONCURRENCY) -> list[dict]:
    """New businesses for the campaign, at most `limit`, searching each query through the container in turn.

    The container has no paging, so each search asks for what is still needed plus the businesses the campaign
    already has (they come back first and are skipped), up to MAX_PLACES_PER_SEARCH. Businesses any campaign
    already has are skipped too.
    """
    seen = maps.existing_businesses(campaign["id"])
    records = []
    for query in queries:
        if len(records) >= limit:
            break
        known = sum(1 for business in seen if business.get("discovery_campaign_id") == campaign["id"])
        wanted = min(limit - len(records) + known, MAX_PLACES_PER_SEARCH)
        print(f"Searching Google Maps through the scraper container: {query} (up to {wanted} places)", flush=True)
        try:
            places = search(query, wanted, url, lang, concurrency)
        except TimeoutError as error:
            print(f"  Skipped this search: {error}.")
            continue
        added = 0
        for place in places:
            if len(records) >= limit:
                break
            record = record_from_place(place, campaign["target_country"])
            if record is not None and maps.accept_record(record, campaign, seen, records):
                added += 1
        print(f"  {len(places)} place(s) found, {added} new.", flush=True)
    return records
