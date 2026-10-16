"""The database side of Modules 1-3 (the browser and LLM parts are covered by their own unit tests)."""
from datetime import datetime, timezone
from unittest import mock

from django.test import TestCase, TransactionTestCase

from pipeline.models import Business, BusinessContact, BusinessSource, BusinessWebsiteProfile, DiscoveryCampaign
from pipeline.services import maps, research, stakeholders
from tests.support import QUALIFICATION_PROMPT, make_business, make_campaign, make_contact, run_command


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

    def test_campaign_prompt_is_the_qualification_criteria(self):
        prompted = make_business(make_campaign(), "A", website_url="https://a.pk")
        bare = make_business(make_campaign(campaign_prompt=None), "B", website_url="https://b.pk")
        rows = {row["id"]: row for row in research.load_businesses(None, None)}
        self.assertEqual(rows[prompted.pk]["qualification_prompt"], QUALIFICATION_PROMPT)
        self.assertIsNone(rows[bare.pk]["qualification_prompt"])
        self.assertFalse(hasattr(research, "research_prompt"))  # the campaign prompt is used as stored

        researched = []
        fake = lambda client, model, business, jina, on_page=None, deadline=None, saved_pages=None: researched.append(business) or {
            "action": "SCORE", "pages_scraped": 1, "scraped_urls": [], "scraped_pages": [], "discovered_links": [],
            "emails": [], "qualification_score": 80, "qualification_reasons": ["fits"], "agent_reasoning": "ok"}
        with mock.patch.object(research, "make_llm_client", return_value=(object(), "m", "test")), \
                mock.patch.object(research, "research_business", fake):
            result = run_command("research_websites", "--local-only")
            self.assertEqual(result.returncode, 1)  # a business was skipped
            self.assertIn(f"Business {bare.pk} (B) skipped: its campaign has no campaign prompt", result.stdout)
            self.assertEqual([b["id"] for b in researched], [prompted.pk])
            self.assertEqual(researched[0]["qualification_prompt"], QUALIFICATION_PROMPT)
            self.assertEqual(BusinessWebsiteProfile.objects.get(business=prompted).qualification_score, 80)
            self.assertFalse(BusinessWebsiteProfile.objects.filter(business=bare).exists())  # left for later

            result = run_command("research_websites", "--local-only", "--business-id", bare.pk)
            self.assertEqual(result.returncode, 1)
            self.assertIn("their campaigns have no campaign prompt", result.stderr)

    def test_team_size_and_languages_are_saved_and_shown(self):
        business = make_business(make_campaign(), "Clinic", website_url="https://clinic.pk")
        profile_id = research.start_profile(business.pk)
        research.finalize_profile(profile_id, {"pages_scraped": 1, "scraped_urls": [], "scraped_pages": [],
                                               "discovered_links": [], "emails": [], "qualification_score": 70,
                                               "qualification_reasons": [], "agent_reasoning": "ok", "team_size": 7,
                                               "languages": ["English", "Urdu"]})
        profile = BusinessWebsiteProfile.objects.get(pk=profile_id)
        self.assertEqual((profile.team_size, profile.languages), (7, ["English", "Urdu"]))
        research.start_profile(business.pk)  # researched again: cleared until the new answer
        self.assertIsNone(BusinessWebsiteProfile.objects.get(pk=profile_id).team_size)

    def test_already_researched_business_explains_redo(self):
        business = make_business(make_campaign(), "Done", website_url="https://done.pk", score=70)
        with mock.patch.object(research, "make_llm_client", return_value=(mock.Mock(), "m", "groq")), \
                mock.patch.object(research, "research_business") as researched:
            result = run_command("research_websites", "--business-id", business.pk)
        self.assertEqual(result.returncode, 0)
        self.assertIn("add --redo", result.stdout)
        researched.assert_not_called()

    def test_needs_an_llm_key(self):
        result = run_command("research_websites", env={"GROQ_API_KEY": "", "GROK_API_KEY": "", "OPENAI_API_KEY": ""})
        self.assertEqual(result.returncode, 1)
        self.assertIn("GROQ_API_KEY", result.stderr)


class ResearchPageTests(TestCase):
    """Reading one page without a browser: its HTML over HTTP plus Jina Reader's rendered text and links."""

    HTML = ('<html><body><nav><a href="/team">Our team</a></nav><p>Call us</p>'
            '<a href="mailto:Info@Clinic.pk">mail</a><a href="https://facebook.com/clinic">fb</a></body></html>')

    def setUp(self):
        patcher = mock.patch.object(research, "is_public_http_url", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_html_and_rendered_page_are_combined(self):
        reader = mock.Mock()
        reader.read.return_value = {"url": "https://clinic.pk/", "title": "Home", "status": 200,
                                    "content": "# Clinic\n\nDr. Ali Khan, owner. Write to ali@clinic.pk",
                                    "links": [("Doctors", "https://clinic.pk/doctors/"), ("Team", "/team")]}
        with mock.patch.object(research, "fetch_html", return_value=("https://www.clinic.pk/", self.HTML)):
            page = research.extract_page("https://clinic.pk/", "https://clinic.pk/", reader)
        self.assertEqual(page["url"], "https://clinic.pk/")
        self.assertIn("Dr. Ali Khan", page["content"])  # Jina's rendered text
        urls = [link["url"] for link in page["links"]]
        self.assertEqual(urls, ["https://clinic.pk/team", "https://facebook.com/clinic", "https://clinic.pk/doctors"])
        self.assertEqual([link["same_site"] for link in page["links"]], [True, False, True])
        self.assertEqual(page["emails"], ["ali@clinic.pk", "info@clinic.pk"])

    def test_either_source_is_enough(self):
        reader = mock.Mock()
        reader.read.side_effect = research.ResearchError("Jina Reader request failed (HTTPError).")
        with mock.patch.object(research, "fetch_html", return_value=("https://clinic.pk/", self.HTML)):
            page = research.extract_page("https://clinic.pk/", "https://clinic.pk/", reader)
        self.assertIn("Call us", page["content"])  # the HTML's own text
        reader.read.side_effect = None
        reader.read.return_value = {"url": "https://clinic.pk/", "title": "", "status": 200, "content": "Rendered only",
                                    "links": []}
        with mock.patch.object(research, "fetch_html", side_effect=research.ResearchError("Page returned HTTP 403.")):
            self.assertEqual(research.extract_page("https://clinic.pk/", "https://clinic.pk/", reader)["content"],
                             "Rendered only")  # a site that blocks plain requests
        reader.read.side_effect = research.ResearchError("Jina Reader request failed (URLError).")
        with mock.patch.object(research, "fetch_html", side_effect=research.ResearchError("Page returned HTTP 403.")), \
                self.assertRaisesRegex(research.ResearchError, "HTTP 403"):
            research.extract_page("https://clinic.pk/", "https://clinic.pk/", reader)

    def test_redirect_off_the_site_and_missing_pages_are_refused(self):
        with mock.patch.object(research, "fetch_html", return_value=("https://parked.example/", self.HTML)), \
                self.assertRaisesRegex(research.ResearchError, "outside the business website"):
            research.extract_page("https://clinic.pk/", "https://clinic.pk/", None)
        reader = mock.Mock()
        reader.read.return_value = {"url": "https://clinic.pk/x", "title": "", "status": 404, "content": "Not found",
                                    "links": []}
        with mock.patch.object(research, "fetch_html", side_effect=research.ResearchError("Page returned HTTP 404.")), \
                self.assertRaisesRegex(research.ResearchError, "HTTP 404"):
            research.extract_page("https://clinic.pk/x", "https://clinic.pk/", reader)

    def test_out_of_time_scores_from_what_was_read(self):
        page = {"url": "https://clinic.pk/", "content": "Dental clinic", "links": [], "emails": []}
        scored = {"action": "SCORE", "summary": "s", "relevant_findings": [], "qualification_score": 55,
                  "qualification_reasons": ["r"], "agent_reasoning": "x"}
        import time

        with mock.patch.object(research, "extract_page", return_value=page), \
                mock.patch.object(research, "validate_agent_response", return_value=scored) as agent:
            research.research_business(mock.Mock(), "m", {"website_url": "https://clinic.pk", "qualification_prompt": "C"},
                                       None, deadline=time.monotonic() + 10)
        self.assertTrue(agent.call_args.args[3])  # must score now
        self.assertEqual(agent.call_args.kwargs["score_note"], research.TIME_LIMIT_NOTE)


class TimeLimitTests(TestCase):
    """With a deadline (the web app) no call runs past it, and unfinished work is left for the next run."""

    def test_ai_call_waits_out_a_rate_limit_only_when_there_is_time(self):
        import time

        import httpx
        from openai import RateLimitError

        from pipeline.management.base import OutOfTime
        from pipeline.services import llm

        def limited(seconds):
            response = httpx.Response(429, headers={"retry-after": str(seconds)},
                                      request=httpx.Request("POST", "https://api.groq.com"))
            return RateLimitError("rate limited", response=response, body=None)

        client = mock.Mock()
        bounded = client.with_options.return_value
        bounded.chat.completions.create.side_effect = [limited(1), "answer"]
        with mock.patch.object(llm.time, "sleep") as sleep:
            self.assertEqual(llm.complete(client, time.monotonic() + 200, model="m"), "answer")
        client.with_options.assert_called_once_with(max_retries=0)  # the SDK's own long waits are off
        sleep.assert_called_once_with(1.0)
        self.assertLessEqual(bounded.chat.completions.create.call_args.kwargs["timeout"], 60)
        bounded.chat.completions.create.side_effect = [limited(60)]
        with self.assertRaises(OutOfTime):
            llm.complete(client, time.monotonic() + 50, model="m")  # waiting 60 s would pass the deadline
        with self.assertRaises(OutOfTime):
            llm.complete(client, time.monotonic() + 3, model="m")  # no time to even start

    def test_research_interrupted_by_the_time_limit_is_done_again_next_run(self):
        from pipeline.management.base import OutOfTime

        campaign = make_campaign()
        first = make_business(campaign, "First", website_url="https://first.pk")
        make_business(campaign, "Second", website_url="https://second.pk")
        with mock.patch.object(research, "make_llm_client", return_value=(object(), "m", "test")), \
                mock.patch.object(research, "research_business", side_effect=OutOfTime("the run's time limit was reached")):
            result = run_command("research_websites", "--local-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("researched again in the next run", result.stdout)
        self.assertIn("Time limit reached: 2 business(es)", result.stdout)
        self.assertEqual(BusinessWebsiteProfile.objects.get(business=first).status, "pending")
        self.assertEqual(len(research.load_businesses(None, None)), 2)  # both are picked up again


class ResumeTests(TestCase):
    """An interrupted research continues where it stopped (each run on Vercel may only get a few AI answers)."""

    PAGE = {"url": "https://clinic.pk/", "content": "Home", "emails": ["a@clinic.pk"], "summary": "Home page",
            "relevant_findings": ["f"], "request_reason": "team page",
            "links": [{"url": "https://clinic.pk/team", "text": "Team", "same_site": True}]}
    SCORED = {"action": "SCORE", "summary": "s", "relevant_findings": [], "qualification_score": 66,
              "qualification_reasons": ["r"], "agent_reasoning": "x"}

    def research(self, saved, pages_read):
        with mock.patch.object(research, "extract_page", side_effect=lambda url, *a: {
                    "url": url, "content": "Team", "links": [], "emails": []}) as extract, \
                mock.patch.object(research, "validate_agent_response", return_value=self.SCORED) as agent:
            result = research.research_business(mock.Mock(), "m", {"website_url": "https://clinic.pk",
                                                                   "qualification_prompt": "C"}, None,
                                                saved_pages=saved)
        self.assertEqual(result["pages_scraped"], pages_read)
        return extract, agent

    def test_continues_with_the_page_the_agent_chose(self):
        saved = [{**self.PAGE, "agent_action": "SCRAPE", "requested_next_url": "https://clinic.pk/team"}]
        extract, agent = self.research(saved, 2)
        extract.assert_called_once()
        self.assertEqual(extract.call_args.args[0], "https://clinic.pk/team")  # not the home page again
        context = agent.call_args.args[2]
        self.assertEqual(context["previously_scraped_pages"][0]["summary"], "Home page")

    def test_decides_on_a_page_read_but_not_decided(self):
        saved = [{**self.PAGE, "agent_action": None, "requested_next_url": None}]
        extract, agent = self.research(saved, 1)
        extract.assert_not_called()  # the saved page is used as it is
        self.assertEqual(agent.call_args.args[2]["current_page_url"], "https://clinic.pk/")

    def test_run_resumes_a_pending_profile_and_redo_starts_over(self):
        business = make_business(make_campaign(), "Clinic", website_url="https://clinic.pk")
        BusinessWebsiteProfile.objects.create(business=business, status="pending", pages_scraped=1,
                                              scraped_pages=[{**self.PAGE, "agent_action": None}])
        seen = []
        fake = lambda client, model, b, jina, on_page=None, deadline=None, saved_pages=None: seen.append(
            len(saved_pages or [])) or {**self.SCORED, "pages_scraped": 1, "scraped_urls": [], "scraped_pages": [],
                                        "discovered_links": [], "emails": []}
        with mock.patch.object(research, "make_llm_client", return_value=(object(), "m", "test")), \
                mock.patch.object(research, "research_business", fake):
            result = run_command("research_websites", "--local-only")
            self.assertIn("continuing after 1 page(s) read before", result.stdout)
            run_command("research_websites", "--local-only", "--redo", "--business-id", business.pk)
        self.assertEqual(seen, [1, 0])


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


def jina_page(url, content="", title="Page", status=200, links=()):
    return {"url": url, "title": title, "content": content, "links": list(links), "status": status}


class CrawlSafetyTests(TestCase):
    def setUp(self):
        self.roles = stakeholders.load_role_config(stakeholders.DEFAULT_ROLES_FILE)

    def test_site_pages_on_private_addresses_are_never_opened(self):
        reader = mock.Mock()
        business = {"website_url": "http://127.0.0.1:8000", "scraped_pages": [], "discovered_links": []}
        evidence = stakeholders.Evidence("Clinic", ["127.0.0.1"])
        self.assertEqual(stakeholders.crawl_site(reader, business, evidence, self.roles, 6), 0)
        reader.read.assert_not_called()

    def test_redirect_to_a_private_address_is_not_read(self):
        reader = mock.Mock()
        reader.read.return_value = jina_page("http://169.254.169.254/latest/meta-data", "Dr. Ali Khan, owner. " * 10)
        business = {"website_url": "https://example.com", "scraped_pages": [], "discovered_links": []}
        evidence = stakeholders.Evidence("Clinic", ["example.com"])
        with mock.patch.object(stakeholders, "is_public_http_url", side_effect=lambda u: "169.254" not in u):
            self.assertEqual(stakeholders.crawl_site(reader, business, evidence, self.roles, 2), 0)
        self.assertEqual(evidence.people, {})

    def test_team_page_names_mailto_and_linkedin(self):
        reader = mock.Mock()
        reader.read.return_value = jina_page(
            "https://example.com/team", "## Our team\n\nDr. Kashif Jamal is the founder and owner of the clinic. " * 3,
            links=[("Email", "mailto:kashif@example.com"), ("LinkedIn", "https://pk.linkedin.com/in/kashif-jamal")])
        business = {"website_url": "https://example.com", "scraped_pages": [], "discovered_links": []}
        evidence = stakeholders.Evidence("Example Clinic", ["example.com"])
        self.assertEqual(stakeholders.crawl_site(reader, business, evidence, self.roles, 1), 1)
        self.assertTrue(evidence.has_target())
        self.assertIn("kashif@example.com", evidence.emails)
        self.assertIn("https://www.linkedin.com/in/kashif-jamal", evidence.linkedin.values())

    def test_missing_pages_and_reader_errors_are_skipped(self):
        reader = mock.Mock()
        reader.read.side_effect = [jina_page("https://example.com/", "x" * 200, status=404),
                                   research.ResearchError("Jina Reader request failed (HTTPError).")]
        business = {"website_url": "https://example.com", "scraped_pages": [], "discovered_links": []}
        evidence = stakeholders.Evidence("Clinic", ["example.com"])
        self.assertEqual(stakeholders.crawl_site(reader, business, evidence, self.roles, 2), 0)


class SearchTests(TestCase):
    def setUp(self):
        self.roles = stakeholders.load_role_config(stakeholders.DEFAULT_ROLES_FILE)

    def test_snippets_that_mention_the_business_give_people(self):
        results = [
            {"url": "https://fellows.ted.com/fellows/1", "title": "Awab Alvi - TED Fellows", "engine": "google cse",
             "content": "Dentist. Organization. Alvi Dental Hospital. He is the founder of Alvi Dental Hospital."},
            {"url": "https://pk.linkedin.com/in/awabalvi", "title": "Awab Alvi - Orthodontist - LinkedIn",
             "engine": "google cse", "content": "Owner. Alvi Dental Hospital. Jan 1999 - Present."},
            {"url": "https://other.example/x", "title": "Unrelated", "engine": "bing",
             "content": "Dr. Sara Malik is the owner of Smile Studio Lahore."},
        ]
        evidence = stakeholders.Evidence("Alvi Dental Hospital", ["alvidental.com"])
        with mock.patch.object(stakeholders.searxng, "search", return_value=results):
            self.assertTrue(stakeholders.run_search('"Alvi Dental Hospital" owner', evidence, self.roles))
        names = {person["name"] for person in evidence.people.values()}
        self.assertIn("Awab Alvi", names)
        self.assertNotIn("Sara Malik", names)  # the snippet is about another business
        self.assertIn("https://www.linkedin.com/in/awabalvi", evidence.linkedin.values())

    def test_branch_suffixes_are_left_out_of_searches(self):
        for name, expected in (("Alvi Dental Hospital — SMCHS", "Alvi Dental Hospital"),
                               ("Smile Studio | DHA Phase 6", "Smile Studio"), ("City Dental (Gulberg)", "City Dental"),
                               ("Dental Care - Lahore", "Dental Care"), ("Kashif Dental Clinic", "Kashif Dental Clinic")):
            self.assertEqual(stakeholders.search_name(name), expected)
        queries = stakeholders.build_queries({"name": "Alvi Dental Hospital — SMCHS", "city": "Karachi"})
        self.assertEqual(queries[1], '"Alvi Dental Hospital" owner')

    def test_unreachable_search_is_reported_not_raised(self):
        evidence = stakeholders.Evidence("Clinic", ["example.com"])
        with mock.patch.object(stakeholders.searxng, "search",
                               side_effect=stakeholders.searxng.SearchUnavailable("SearXNG is not reachable")):
            self.assertFalse(stakeholders.run_search("q", evidence, self.roles))


class NavigationRetryTests(TestCase):
    def test_network_errors_are_retried_and_explained(self):
        page = mock.Mock()
        page.goto.side_effect = [maps.PlaywrightError("Page.goto: net::ERR_CONNECTION_CLOSED"), None]
        with mock.patch.object(maps.time, "sleep"):
            maps.open_page(page, "https://www.google.com/maps/search/x")
        self.assertEqual(page.goto.call_count, 2)
        page.goto.side_effect = maps.PlaywrightError("Page.goto: net::ERR_CONNECTION_CLOSED")
        with mock.patch.object(maps.time, "sleep"), self.assertRaises(maps.PlaywrightError) as raised:
            maps.open_page(page, "https://www.google.com/maps/search/x")
        self.assertIn("could not be reached", maps.browser_hint(raised.exception))
        self.assertIn("playwright install", maps.browser_hint(maps.PlaywrightError("Executable doesn't exist at /x")))
        self.assertIsNone(maps.browser_hint(maps.PlaywrightError("Timeout 60000ms exceeded")))

    def test_a_page_reached_twice_through_redirects_is_read_once(self):
        reader = mock.Mock()
        reader.read.return_value = jina_page("https://example.com/about-us/", "About our clinic. " * 20, "About")
        business = {"website_url": "https://example.com", "scraped_pages": [], "discovered_links": []}
        evidence = stakeholders.Evidence("Clinic", ["example.com"])
        roles = stakeholders.load_role_config(stakeholders.DEFAULT_ROLES_FILE)
        self.assertEqual(stakeholders.crawl_site(reader, business, evidence, roles, 4), 1)
        self.assertEqual(reader.read.call_count, 4)


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
