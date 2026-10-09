"""The database side of Modules 1-3 (the browser and LLM parts are covered by their own unit tests)."""
from datetime import datetime, timezone
from unittest import mock

from django.test import TestCase, TransactionTestCase

from pipeline.models import Business, BusinessContact, BusinessSource, BusinessWebsiteProfile, DiscoveryCampaign
from pipeline.services import maps, research, stakeholders
from tests.support import make_business, make_campaign, make_contact, run_command


class CampaignTests(TestCase):
    def test_create_campaign_from_options(self):
        result = run_command("create_campaign", "--name", "Lahore dentists", "--country", "Pakistan",
                             "--locations", "Lahore, DHA", "--search-terms", "dental clinic,dentist")
        self.assertEqual(result.returncode, 0, result.stderr)
        campaign = DiscoveryCampaign.objects.get()
        self.assertEqual((campaign.name, campaign.target_country, campaign.target_locations, campaign.search_terms),
                         ("Lahore dentists", "Pakistan", ["Lahore", "DHA"], ["dental clinic", "dentist"]))
        self.assertIn(f"Campaign created: id={campaign.pk}", result.stdout)

    def test_create_campaign_needs_a_name_without_a_terminal(self):
        self.assertEqual(run_command("create_campaign").returncode, 1)
        self.assertEqual(run_command("create_campaign", "--name", "x", "--country", "A, B").returncode, 1)
        self.assertFalse(DiscoveryCampaign.objects.exists())

    def test_fetch_needs_campaign_and_limit_without_a_terminal(self):
        campaign = make_campaign(search_terms=["dentist"], target_locations=["Lahore"])
        no_campaign = run_command("fetch_businesses")
        self.assertEqual(no_campaign.returncode, 1)
        self.assertIn("--campaign-id", no_campaign.stdout)
        no_limit = run_command("fetch_businesses", "--campaign-id", campaign.pk)
        self.assertIn("--limit", no_limit.stdout)
        missing = run_command("fetch_businesses", "--campaign-id", 999999, "--limit", 1)
        self.assertIn("does not exist", missing.stdout)

    def test_fetch_stores_new_businesses_and_marks_the_campaign(self):
        campaign = make_campaign(target_country="Pakistan", search_terms=["dentist"], target_locations=["Lahore"])
        make_business(campaign, "Known", google_place_id="ChIJknown")
        now = datetime.now(timezone.utc)
        records = [{"business": {"name": "New Clinic", "google_place_id": "ChIJnew", "google_maps_url": "https://maps/x",
                                 "discovery_campaign_id": campaign.pk, "first_discovered_at": now,
                                 "last_discovered_at": now, "opening_hours": ["Mon 9-5"], "google_rating": 4.5},
                    "raw_data": {"name": "New Clinic"}}]
        with mock.patch.object(maps, "sync_playwright"), mock.patch.object(maps, "scrape_campaign", return_value=records) as scrape:
            result = run_command("fetch_businesses", "--campaign-id", campaign.pk, "--limit", 5, "--delay", 0)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(scrape.call_args.args[2], ["dentist in Lahore, Pakistan"])
        business = Business.objects.get(name="New Clinic")
        self.assertEqual(business.opening_hours, ["Mon 9-5"])
        source = BusinessSource.objects.get()
        self.assertEqual((source.business, source.source_business_id, source.raw_data), (business, "ChIJnew", {"name": "New Clinic"}))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "completed")
        self.assertIsNotNone(campaign.completed_at)
        self.assertEqual(maps.existing_businesses(campaign.pk)[0]["google_place_id"], "ChIJknown")

    def test_failed_fetch_marks_the_campaign_failed(self):
        campaign = make_campaign(search_terms=["dentist"], target_locations=["Lahore"])
        with mock.patch.object(maps, "sync_playwright", side_effect=RuntimeError("no browser")):
            result = run_command("fetch_businesses", "--campaign-id", campaign.pk, "--limit", 5)
        self.assertEqual(result.returncode, 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "failed")


class ResearchTests(TestCase):
    def test_queue_profile_lifecycle(self):
        campaign = make_campaign()
        pending = make_business(campaign, "A", website_url="https://a.pk")
        make_business(campaign, "No site")
        failed = make_business(campaign, "B", website_url="https://b.pk")
        BusinessWebsiteProfile.objects.create(business=failed, status="failed")
        done = make_business(campaign, "C", website_url="https://c.pk", score=70)
        self.assertEqual([b["id"] for b in research.load_businesses(None, None)], [pending.pk])
        self.assertEqual([b["id"] for b in research.load_businesses(None, None, retry_failed=True)], [pending.pk, failed.pk])
        self.assertEqual(research.load_businesses(None, [done.pk]), [])
        self.assertEqual([b["id"] for b in research.load_businesses(None, [done.pk], redo=True)], [done.pk])
        self.assertEqual(len(research.load_businesses(None, None, redo=True)), 3)  # every business with a website
        BusinessWebsiteProfile.objects.filter(business=done).update(status="running")
        self.assertEqual(research.load_businesses(None, [done.pk], redo=True), [])  # never one being researched now

        profile_id = research.start_profile(pending.pk)
        research.save_progress(profile_id, [{"url": "https://a.pk/"}], {"x@a.pk"}, [{"url": "https://a.pk/about"}])
        profile = BusinessWebsiteProfile.objects.get(pk=profile_id)
        self.assertEqual((profile.status, profile.pages_scraped, profile.emails), ("running", 1, ["x@a.pk"]))
        research.finalize_profile(profile_id, {"pages_scraped": 1, "scraped_urls": ["https://a.pk/"], "scraped_pages": [],
                                               "discovered_links": [], "emails": [], "qualification_score": 66,
                                               "qualification_reasons": ["ok"], "agent_reasoning": "fine"})
        profile.refresh_from_db()
        self.assertEqual((profile.status, profile.qualification_score), ("completed", 66))
        research.fail_profile(research.start_profile(failed.pk), ValueError("bad"))
        self.assertEqual(BusinessWebsiteProfile.objects.get(business=failed).agent_reasoning, "ValueError: bad")

    def test_already_researched_business_explains_redo(self):
        business = make_business(make_campaign(), "Done", website_url="https://done.pk", score=70)
        with mock.patch.object(research, "make_llm_client", return_value=(mock.Mock(), "m", "groq")), \
                mock.patch.object(research, "sync_playwright") as playwright:
            result = run_command("research_websites", "--business-id", business.pk)
        self.assertEqual(result.returncode, 0)
        self.assertIn("add --redo", result.stdout)
        playwright.assert_not_called()

    def test_needs_an_llm_key(self):
        result = run_command("research_websites", env={"GROQ_API_KEY": "", "GROK_API_KEY": "", "OPENAI_API_KEY": ""})
        self.assertEqual(result.returncode, 1)
        self.assertIn("GROQ_API_KEY", result.stderr)


class StakeholderTests(TestCase):
    def test_queue_and_saving_contacts(self):
        campaign = make_campaign()
        good = make_business(campaign, "Good", score=80, website_url="https://good.pk")
        BusinessWebsiteProfile.objects.filter(business=good).update(emails=["x@good.pk"], scraped_urls=["https://good.pk/"])
        make_business(campaign, "Low", score=20)
        queue = stakeholders.load_businesses(50, None, None, False)
        self.assertEqual([(b["id"], b["site_emails"], b["qualification_score"]) for b in queue], [(good.pk, ["x@good.pk"], 80)])

        manual = make_contact(good, "boss@good.pk", name="Manual")
        BusinessContact.objects.filter(pk=manual.pk).update(email_source="manual")
        make_contact(good, "old@good.pk", name="Old guess")
        rows = [{"business_id": good.pk, "name": "Shoaib Ahmed", "first_name": "Shoaib", "last_name": "Ahmed",
                 "job_title": "Owner", "role_type": "owner", "email": "shoaib@good.pk", "email_source": "inferred",
                 "email_status": "unverified", "linkedin_url": None, "source_urls": ["https://good.pk/"],
                 "confidence": 0.425, "candidate_emails": [{"email": "s@good.pk"}], "is_primary": True,
                 "discovery_reasoning": "why"}]
        stakeholders.save_contacts(good.pk, rows)
        self.assertEqual(sorted(BusinessContact.objects.values_list("name", flat=True)), ["Manual", "Shoaib Ahmed"])
        profile = BusinessWebsiteProfile.objects.get(business=good)
        self.assertEqual(profile.contact_discovery_status, "completed")
        self.assertEqual(stakeholders.load_businesses(50, None, None, False), [])  # done
        self.assertEqual(len(stakeholders.load_businesses(50, None, [good.pk], True)), 1)  # --redo
        stakeholders.save_contacts(good.pk, [])
        self.assertEqual(BusinessWebsiteProfile.objects.get(business=good).contact_discovery_status, "no_contacts")

    def test_dry_run_without_browser(self):
        campaign = make_campaign()
        business = make_business(campaign, "Kashif Dental Clinic", score=80, website_url="https://kashif.pk")
        BusinessWebsiteProfile.objects.filter(business=business).update(scraped_pages=[
            {"url": "https://kashif.pk/about", "content": "Dr. Kashif Jamal is the founder and owner of the clinic."}])
        result = run_command("find_stakeholders", "--no-search", "--no-crawl", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Kashif Jamal", result.stdout)
        self.assertFalse(BusinessContact.objects.exists())
        saved = run_command("find_stakeholders", "--no-search", "--no-crawl")
        self.assertEqual(saved.returncode, 0)
        contact = BusinessContact.objects.get()
        self.assertEqual((contact.first_name, contact.is_primary), ("Kashif", True))
        self.assertTrue(contact.candidate_emails)


class CrawlSafetyTests(TestCase):
    def test_site_pages_on_private_addresses_are_never_opened(self):
        driver = mock.Mock()
        business = {"website_url": "http://127.0.0.1:8000", "scraped_pages": [], "discovered_links": []}
        evidence = stakeholders.Evidence("Clinic", ["127.0.0.1"])
        roles = stakeholders.load_role_config(stakeholders.DEFAULT_ROLES_FILE)
        with mock.patch.object(stakeholders.time, "sleep"):
            self.assertEqual(stakeholders.crawl_site(driver, business, evidence, roles, 6), 0)
        driver.get.assert_not_called()

    def test_redirect_to_a_private_address_is_not_read(self):
        driver = mock.Mock()
        driver.current_url = "http://169.254.169.254/latest/meta-data"
        business = {"website_url": "https://example.com", "scraped_pages": [], "discovered_links": []}
        evidence = stakeholders.Evidence("Clinic", ["example.com"])
        roles = stakeholders.load_role_config(stakeholders.DEFAULT_ROLES_FILE)
        with mock.patch.object(stakeholders.time, "sleep"), \
                mock.patch.object(stakeholders, "is_public_http_url", side_effect=lambda u: "169.254" not in u):
            self.assertEqual(stakeholders.crawl_site(driver, business, evidence, roles, 2), 0)
        driver.find_element.assert_not_called()


class PlaywrightDatabaseTests(TransactionTestCase):
    """Database calls made while a real Playwright session is open (its event loop used to make Django refuse them)."""

    def tearDown(self):
        from pipeline.services.dbthread import close_worker_connection

        close_worker_connection()

    def test_research_and_maps_can_save_while_the_browser_is_open(self):
        from playwright.sync_api import sync_playwright

        campaign = make_campaign()
        business = make_business(campaign, "Clinic", website_url="https://clinic.pk", google_place_id="ChIJx")
        with sync_playwright():
            profile_id = research.start_profile(business.pk)
            research.save_progress(profile_id, [{"url": "https://clinic.pk/"}], {"a@clinic.pk"}, [])
            research.finalize_profile(profile_id, {"pages_scraped": 1, "scraped_urls": [], "scraped_pages": [],
                                                   "discovered_links": [], "emails": [], "qualification_score": 61,
                                                   "qualification_reasons": [], "agent_reasoning": "ok"})
            maps.update_campaign_status(campaign.pk, "running")
            seen = maps.existing_businesses(campaign.pk)
        self.assertEqual(BusinessWebsiteProfile.objects.get(pk=profile_id).qualification_score, 61)
        self.assertEqual(seen[0]["google_place_id"], "ChIJx")
        self.assertEqual(DiscoveryCampaign.objects.get(pk=campaign.pk).status, "running")


class MapsDetailsTests(TestCase):
    def test_parse_address(self):
        cases = {
            "Building, Room # 10-14, Block 3 Gulshan-e-Iqbal, Karachi, 75300, Pakistan": ("Karachi", None, "75300"),
            "Shop 2, Main Blvd, Gulberg III, Lahore, Punjab 54000, Pakistan": ("Lahore", "Punjab", "54000"),
            "100 Congress Ave, Austin, TX 78701, United States": ("Austin", "TX", "78701"),
            "10 Downing St, London SW1A 2AA, UK": ("London", None, "SW1A 2AA"),
            "Plot 5, F-7 Markaz, Islamabad, Pakistan": ("Islamabad", None, None),
            "B-256 karachi": (None, None, None),
            "": (None, None, None),
        }
        for address, expected in cases.items():
            parsed = maps.parse_address(address, "Pakistan")
            self.assertEqual((parsed["city"], parsed["state"], parsed["postal_code"]), expected, address)

    def test_parse_review_count(self):
        self.assertEqual(maps.parse_review_count("4.7\n(275)"), 275)
        self.assertEqual(maps.parse_review_count("4.2(1,234)"), 1234)
        self.assertEqual(maps.parse_review_count(None, "1,275 reviews"), 1275)
        self.assertIsNone(maps.parse_review_count("4.7", None))
        self.assertTrue(maps.limited_view({"google_rating": 4.7, "google_review_count": None}))
        self.assertFalse(maps.limited_view({"google_rating": 4.7, "google_review_count": 12}))
        self.assertFalse(maps.limited_view({"google_rating": None, "google_review_count": None}))  # no reviews at all

    def test_icons_are_removed_from_text(self):
        self.assertEqual(maps.normalize_text("Friday 10 AM"), "Friday 10 AM")

    def test_apply_details_updates_maps_fields_but_only_fills_the_rest(self):
        week = [f"Day{i} 9-5" for i in range(7)]
        business = make_business(make_campaign(), "Clinic", website_url="https://keep.pk", opening_hours=week,
                                 address="X, Karachi, 75300, Pakistan")
        changed = maps.apply_details(business.pk, {
            "category": "Dental clinic", "google_rating": 4.7, "google_review_count": 275,
            "opening_hours": ["Friday 2-9 PM"],  # partial: must not replace the full week
            "website_url": "https://other.pk", "phone": "+923001234567"},
            maps.parse_address(business.address, "Pakistan"))
        business.refresh_from_db()
        self.assertEqual((business.category, business.google_review_count, str(business.google_rating)),
                         ("Dental clinic", 275, "4.7"))
        self.assertEqual(business.opening_hours, week)
        self.assertEqual(business.website_url, "https://keep.pk")  # never replaced
        self.assertEqual(business.phone, "+923001234567")  # filled in
        self.assertEqual((business.city, business.postal_code), ("Karachi", "75300"))
        self.assertIn("google_review_count", changed)
        self.assertEqual(maps.apply_details(business.pk, {"google_rating": 4.7, "google_review_count": 275},
                                            maps.parse_address(business.address, "Pakistan")), ["last_discovered_at"])

    def test_refresh_from_addresses_without_a_browser(self):
        campaign = make_campaign()
        karachi = make_business(campaign, "A", address="Shop 1, Karachi, 75300, Pakistan", country="Pakistan")
        make_business(campaign, "B", address="Lahore, Punjab 54000, Pakistan", country="Pakistan", city="Lahore",
                      category="Dentist", google_review_count=5)  # complete: skipped without --all
        with mock.patch.object(maps, "sync_playwright") as playwright:
            result = run_command("refresh_businesses", "--address-only")
        playwright.assert_not_called()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Refreshing 1 business(es) from their addresses", result.stdout)
        karachi.refresh_from_db()
        self.assertEqual((karachi.city, karachi.postal_code), ("Karachi", "75300"))

    def test_fetched_businesses_get_city_and_state(self):
        details = {"name": "X", "address": "Shop 2, Lahore, Punjab 54000, Pakistan"}
        self.assertEqual(maps.parse_address(details["address"])["state"], "Punjab")
