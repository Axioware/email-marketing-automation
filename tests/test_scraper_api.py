"""The second lead source: the Google Maps scraper container, against a fake container on a local port."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from pipeline.models import Business, BusinessSource, DiscoveryCampaign, PipelineRun
from pipeline.services import maps, scraper_api
from tests.support import make_business, make_campaign, run_command

# What the container answered for "dental clinic in Lahore, Pakistan" (trimmed).
PLACES = [
    {"name": "DH Dental Clinic", "place_id": "ChIJVzVJWvYDGTkRU2HO9KvXO6w",
     "coordinates": {"latitude": 31.4797571, "longitude": 74.2804939},
     "address": "152 G1 Canal Rd, Block G1 Block G 1 Phase 1 Johar Town, Lahore, 54590, Pakistan",
     "rating": 4.9, "reviews_count": 79, "categories": ["Dental clinic"], "website": "http://www.dh.dental/",
     "phone": "923223368251", "hours": ["Wednesday, 10 AM to 6 PM"],
     "link": "https://www.google.com/maps/place/DH+Dental+Clinic/data=!4m7"},
    {"name": "Hafiz dental clinic", "place_id": "ChIJ94RWX1sFGTkRKp6_6A4Hz58",
     "coordinates": {"latitude": 31.4891437, "longitude": 74.369171},
     "address": "Main Walton Rd, Shah Taj Colony, Lahore, 54000, Pakistan", "rating": 4.8, "reviews_count": 323,
     "categories": ["Dental clinic"], "website": "https://hafizdentalclinic.com/", "phone": "923004374908",
     "link": "https://www.google.com/maps/place/Hafiz+dental+clinic/data=!4m7"},
]


class FakeContainer:
    """GET / answers; GET /scrape-get answers `places` (or `status`), recording each request's parameters."""

    def __init__(self, places=PLACES, status=200, key=None):
        self.places, self.status, self.requests, self.key = places, status, [], key
        container = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                if container.key is not None and self.headers.get("X-API-Key") != container.key:
                    code, body = 401, {"detail": "Wrong or missing X-API-Key header."}
                elif url.path == "/scrape-get":
                    container.requests.append({key: values[0] for key, values in parse_qs(url.query).items()})
                    code, body = container.status, container.places if container.status == 200 else {"detail": "boom"}
                else:
                    code, body = 200, {"message": "Google Maps Scraper API is running."}
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ScraperCase(TestCase):
    def setUp(self):
        self.container = FakeContainer()
        self.addCleanup(self.container.close)
        self.campaign = make_campaign(target_country="Pakistan", search_terms=["dental clinic"], target_locations=["Lahore"],
                                      lead_source="scraper_api")

    def fetch(self, *args):
        return run_command("fetch_businesses", "--campaign-id", self.campaign.pk, *args,
                           env={"MAPS_SCRAPER_URL": self.container.url})


class RecordTests(TestCase):
    def test_a_place_becomes_the_same_record_the_browser_makes(self):
        business = scraper_api.record_from_place(PLACES[0], "Pakistan")["business"]
        self.assertEqual(business["name"], "DH Dental Clinic")
        self.assertEqual(business["category"], "Dental clinic")
        self.assertEqual((business["city"], business["postal_code"], business["country"]), ("Lahore", "54590", "Pakistan"))
        self.assertEqual(business["phone"], "+923223368251")  # digits with the country code, read as international
        self.assertEqual((business["google_rating"], business["google_review_count"]), (4.9, 79))
        self.assertEqual((business["latitude"], business["longitude"]), (31.4797571, 74.2804939))
        self.assertEqual(business["domain"], "dh.dental")
        self.assertEqual(business["opening_hours"], ["Wednesday, 10 AM to 6 PM"])
        self.assertEqual((business["source"], business["google_place_id"]), ("google_maps_scraper", PLACES[0]["place_id"]))
        self.assertEqual(set(business) - {"source", "source_url"},
                         {"name", "category", "website_url", "domain", "phone", "address", "city", "state",
                          "postal_code", "country", "latitude", "longitude", "google_place_id", "google_maps_url",
                          "google_rating", "google_review_count", "opening_hours"})
        self.assertIsNone(scraper_api.record_from_place({"name": " "}, "Pakistan"))  # nameless: skipped
        sparse = scraper_api.record_from_place({"name": "X", "rating": "n/a", "phone": "042 111 222 333"}, "Pakistan")
        self.assertIsNone(sparse["business"]["google_rating"])
        self.assertTrue(sparse["business"]["phone"].startswith("+92"))  # a local number, read with the country


class FetchTests(ScraperCase):
    def test_fetch_saves_new_businesses_and_skips_known_ones(self):
        make_business(self.campaign, "DH Dental Clinic", google_place_id=PLACES[0]["place_id"])
        result = self.fetch("--limit", 5, "--concurrency", 3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Lead source: scraper container.", result.stdout)
        self.assertIn("2 place(s) found, 1 new.", result.stdout)
        request = self.container.requests[0]
        self.assertEqual(request["query"], "dental clinic in Lahore, Pakistan")
        self.assertEqual((request["max_places"], request["concurrency"], request["lang"]), ("6", "3", "en"))  # 5 + 1 known
        hafiz = Business.objects.get(name="Hafiz dental clinic")
        self.assertEqual((hafiz.source, hafiz.google_review_count, hafiz.city), ("google_maps_scraper", 323, "Lahore"))
        source = BusinessSource.objects.get(business=hafiz)
        self.assertEqual((source.source, source.source_business_id), ("google_maps_scraper", PLACES[1]["place_id"]))
        self.assertEqual(source.raw_data["reviews_count"], 323)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, "completed")
        self.assertIn("No new businesses", self.fetch("--limit", 5).stdout)  # a rerun adds nothing twice

    def test_source_option_overrides_the_campaign(self):
        DiscoveryCampaign.objects.filter(pk=self.campaign.pk).update(lead_source="browser")
        result = self.fetch("--limit", 1, "--source", "scraper_api")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(Business.objects.count(), 1)  # stops at the limit
        with mock.patch.object(maps, "scrape_with_browser", return_value=[]) as browser:
            self.fetch("--limit", 1)  # the campaign's own source: the built-in browser
        browser.assert_called_once()
        self.assertEqual(len(self.container.requests), 1)

    def test_container_down_or_timing_out(self):
        self.container.close()
        result = self.fetch("--limit", 5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not reachable", result.stdout)
        self.assertIn("MAPS_SCRAPER_URL", result.stdout)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, "pending")  # nothing started

        slow = FakeContainer(status=504)
        self.addCleanup(slow.close)
        self.container = slow
        result = self.fetch("--limit", 5)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Skipped this search: the container gave up after 30 minutes", result.stdout)

        broken = FakeContainer(status=500)
        self.addCleanup(broken.close)
        self.container = broken
        result = self.fetch("--limit", 5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("answered HTTP 500 boom", result.stdout)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, "failed")

    def test_a_search_never_asks_for_more_than_the_container_can_do_in_time(self):
        self.fetch("--limit", 500)
        self.assertEqual(self.container.requests[0]["max_places"], str(scraper_api.MAX_PLACES_PER_SEARCH))
        self.assertEqual(run_command("fetch_businesses", "--campaign-id", self.campaign.pk, "--limit", 1,
                                     "--concurrency", 50).returncode, 1)


class KeyTests(TestCase):
    """The container behind nginx answers only requests with X-API-Key = MAPS_SCRAPER_KEY."""

    def setUp(self):
        self.container = FakeContainer(key="s3cret")
        self.addCleanup(self.container.close)

    def test_key_is_sent(self):
        with mock.patch.dict("os.environ", {"MAPS_SCRAPER_KEY": "s3cret"}):
            self.assertIsNone(scraper_api.status(self.container.url))
            self.assertEqual(len(scraper_api.search("dentist", 2, self.container.url)), 2)

    def test_wrong_or_missing_key_is_reported(self):
        for env in ({"MAPS_SCRAPER_KEY": "wrong"}, {"MAPS_SCRAPER_KEY": ""}):
            with mock.patch.dict("os.environ", env):
                self.assertIn("refused the key", scraper_api.status(self.container.url))
                with self.assertRaisesRegex(scraper_api.ScraperUnavailable, "refused the key"):
                    scraper_api.search("dentist", 2, self.container.url)
        self.assertEqual(self.container.requests, [])


class AdminAndApiTests(ScraperCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("leads", "l@example.org", "pw"))

    def test_dashboard_offers_both_sources(self):
        with mock.patch.dict("os.environ", {"MAPS_SCRAPER_URL": self.container.url}):
            page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("Get leads", page)
        self.assertIn("running", page)
        self.assertIn(f"--source+browser", page)
        self.assertIn(f"--source+scraper_api", page)
        self.container.close()
        with mock.patch.dict("os.environ", {"MAPS_SCRAPER_URL": self.container.url}):
            page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("A campaign gets its leads from the scraper container, but it is not reachable", page)

    def test_campaign_page_buttons_actions_and_field(self):
        url = reverse("admin:pipeline_discoverycampaign_change", args=[self.campaign.pk])
        page = self.client.get(url).content.decode()
        self.assertIn("Fetch with built-in browser", page)
        self.assertIn("Fetch with scraper container", page)
        self.assertIn('name="lead_source"', page)
        response = self.client.post(reverse("admin:pipeline_discoverycampaign_changelist"),
                                    {"action": "fetch_with_scraper", "_selected_action": [self.campaign.pk]})
        self.assertIn("--source+scraper_api", response["Location"])

    @override_settings(AUTH_TOKEN="leads-key")
    def test_api_source_options(self):
        api = APIClient()
        api.credentials(HTTP_AUTH="leads-key")
        self.assertEqual(api.get(f"/api/campaigns/{self.campaign.pk}/").json()["lead_source"], "scraper_api")
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            response = api.post(f"/api/campaigns/{self.campaign.pk}/fetch-businesses/",
                                {"limit": 7, "source": "browser", "concurrency": 2}, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(PipelineRun.objects.get().arguments,
                         ["--campaign-id", str(self.campaign.pk), "--limit", "7", "--source", "browser",
                          "--concurrency", "2"])
        bad = api.post(f"/api/campaigns/{self.campaign.pk}/fetch-businesses/", {"source": "bing"}, format="json")
        self.assertEqual(bad.status_code, 400)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            api.post(f"/api/campaigns/{self.campaign.pk}/run-pipeline/", {"source": "scraper_api"}, format="json")
        self.assertIn("--source", PipelineRun.objects.latest("id").arguments)

    def test_full_pipeline_passes_the_source_to_fetching(self):
        with mock.patch("pipeline.services.full_pipeline.call_command") as call:
            run_command("run_pipeline", "--campaign-id", self.campaign.pk, "--fetch-limit", 3, "--source", "scraper_api",
                        "--skip", "research", "--skip", "stakeholders", "--skip", "verify", "--skip", "generate",
                        "--skip", "followups")
        self.assertEqual(call.call_args_list[0].args,
                         ("fetch_businesses", "--campaign-id", str(self.campaign.pk), "--limit", "3", "--source",
                          "scraper_api"))


class CrossCampaignTests(ScraperCase):
    def test_a_business_another_campaign_has_is_skipped_and_reported(self):
        other = make_campaign(name="Lahore clinics", target_country="Pakistan")
        make_business(other, "DH Dental Clinic", google_place_id=PLACES[0]["place_id"])
        make_business(other, "Hafiz dental clinic", address=PLACES[1]["address"])  # older row, no place id
        result = self.fetch("--limit", 5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Skipped DH Dental Clinic: already in campaign "Lahore clinics".', result.stdout)
        self.assertIn('Skipped Hafiz dental clinic: already in campaign "Lahore clinics".', result.stdout)
        self.assertIn("No new businesses", result.stdout)
        self.assertFalse(Business.objects.filter(discovery_campaign=self.campaign).exists())
        self.assertEqual(self.container.requests[0]["max_places"], "5")  # only this campaign's businesses count

    def test_one_business_per_google_place(self):
        from django.db import IntegrityError, transaction

        make_business(self.campaign, "First", google_place_id="ChIJsame")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_business(make_campaign(name="Other"), "Second", google_place_id="ChIJsame")
        make_business(self.campaign, "No place id A", google_place_id=None)
        make_business(self.campaign, "No place id B", google_place_id=None)  # several without one are fine
        make_business(self.campaign, "Empty A", google_place_id="")
        make_business(self.campaign, "Empty B", google_place_id="")

    def test_a_place_saved_by_another_run_meanwhile_is_skipped(self):
        make_business(make_campaign(name="Other"), "Raced", google_place_id=PLACES[0]["place_id"])
        record = scraper_api.record_from_place(PLACES[0], "Pakistan")
        maps.accept_record(record, {"id": self.campaign.pk}, [], records := [])
        second = scraper_api.record_from_place(PLACES[1], "Pakistan")
        maps.accept_record(second, {"id": self.campaign.pk}, [], records)
        self.assertEqual(maps.save_records(records), 1)  # the raced one is skipped, the other saved
        self.assertTrue(Business.objects.filter(name="Hafiz dental clinic", discovery_campaign=self.campaign).exists())


class PlaceIdTests(TestCase):
    def test_place_id_is_read_from_a_maps_link(self):
        url = ("https://www.google.com/maps/place/Clinic/data=!4m7!3m6!1s0x3eb338acf76:0x1!8m2!3d24.9!4d67.1"
               "!16s%2Fg%2F11x!19sChIJt1Zu96w4sz4R2EwodF7D35U?authuser=0&hl=en")
        self.assertEqual(maps.place_id_from_url(url), "ChIJt1Zu96w4sz4R2EwodF7D35U")  # no word boundary before it
        self.assertIsNone(maps.place_id_from_url("https://www.google.com/maps/search/dentist"))
        self.assertIsNone(maps.place_id_from_url(None))
        place = {**PLACES[0], "place_id": None}
        self.assertEqual(scraper_api.record_from_place({**place, "link": url}, "Pakistan")["business"]["google_place_id"],
                         "ChIJt1Zu96w4sz4R2EwodF7D35U")

    def test_backfill_migration(self):
        from importlib import import_module

        from django.apps import apps

        backfill = import_module("pipeline.migrations.0013_backfill_place_ids").backfill_place_ids
        link = "https://www.google.com/maps/place/X/data=!4m7!19sChIJaaaaaaaaaaaaaaaa?hl=en"
        campaign = make_campaign()
        old = make_business(campaign, "Old", google_maps_url=link)
        BusinessSource.objects.create(business=old, source="google_maps", raw_data={})
        twin = make_business(campaign, "Twin", google_maps_url=link)  # same place: left empty
        make_business(campaign, "Has one", google_place_id="ChIJbbbbbbbbbbbbbbbb", google_maps_url=link.replace("a", "b"))
        backfill(apps, None)
        old.refresh_from_db()
        twin.refresh_from_db()
        self.assertEqual(old.google_place_id, "ChIJaaaaaaaaaaaaaaaa")
        self.assertEqual(old.sources.get().source_business_id, "ChIJaaaaaaaaaaaaaaaa")
        self.assertIsNone(twin.google_place_id)


@override_settings(AUTH_TOKEN="place-key")
class PlaceIdApiTests(TestCase):
    def test_api_refuses_a_second_business_for_the_same_place(self):
        campaign = make_campaign()
        make_business(campaign, "First", google_place_id="ChIJcccccccccccccccc")
        api = APIClient()
        api.credentials(HTTP_AUTH="place-key")
        response = api.post("/api/businesses/", {"discovery_campaign": campaign.pk, "name": "Second",
                                                 "google_place_id": "ChIJcccccccccccccccc"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        ok = api.post("/api/businesses/", {"discovery_campaign": campaign.pk, "name": "Third"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
