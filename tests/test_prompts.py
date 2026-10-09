"""Campaign prompts and email prompts: storage rules, how emails pick them, the admin pages and the API."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from pipeline.models import CampaignPrompt, DiscoveryCampaign, Email, EmailPrompt, PipelineRun
from pipeline.services import generation as g
from tests.fake_smtp import FakeSmtpServer
from tests.support import make_business, make_campaign, make_email, review_env, run_command
from tests.test_generate_emails import FakeLLM

KEY = "prompt-test-key"


class PromptCase(TestCase):
    def setUp(self):
        self.smtp = FakeSmtpServer()
        self.llm = FakeLLM()
        self.addCleanup(self.smtp.close)
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(os.environ, review_env(self.smtp, self.llm))
        env.start()
        self.addCleanup(env.stop)
        self.user = get_user_model().objects.create_superuser("admin", "a@example.org", "pw")
        self.client.force_login(self.user)

    def prompt_set(self, name="Dental", campaign=None):
        campaign_prompt = CampaignPrompt.objects.create(name=name, prompt=f"CAMPAIGN RULES {name}")
        first = EmailPrompt.objects.create(campaign_prompt=campaign_prompt, name="First touch",
                                           prompt=f"FIRST TOUCH {name}", is_default=True)
        follow = EmailPrompt.objects.create(campaign_prompt=campaign_prompt, name="Follow-up", prompt=f"FOLLOW UP {name}")
        if campaign is not None:
            campaign.campaign_prompt = campaign_prompt
            campaign.save()
        return campaign_prompt, first, follow

    def generate(self, *args):
        return run_command("generate_emails", *args)


class ModelTests(PromptCase):
    def test_seeded_axioware_prompts(self):
        seeded = CampaignPrompt.objects.get(name="Axioware - dental clinics")
        self.assertEqual(seeded.prompt, g.DEFAULT_CAMPAIGN_PROMPT)
        self.assertEqual(seeded.default_email_prompt().prompt, g.DEFAULT_EMAIL_PROMPT)
        self.assertEqual(seeded.default_email_prompt().full_prompt, g.SYSTEM_PROMPT)

    def test_one_default_email_prompt_per_campaign_prompt(self):
        campaign_prompt, first, follow = self.prompt_set()
        follow.is_default = True
        follow.save()
        first.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertEqual(campaign_prompt.default_email_prompt(), follow)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailPrompt.objects.filter(pk=first.pk).update(is_default=True)  # bypassing save() is refused by the database

    def test_a_campaign_prompt_belongs_to_one_campaign(self):
        campaign_prompt, _, _ = self.prompt_set(campaign=make_campaign(name="A"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            DiscoveryCampaign.objects.create(name="B", campaign_prompt=campaign_prompt)

    def test_deleting_a_prompt_keeps_campaigns_and_emails(self):
        email = make_email()
        campaign_prompt, first, _ = self.prompt_set(campaign=email.business.discovery_campaign)
        Email.objects.filter(pk=email.pk).update(email_prompt=first)
        campaign_prompt.delete()
        email.refresh_from_db()
        self.assertIsNone(email.email_prompt)
        self.assertIsNone(DiscoveryCampaign.objects.get(pk=email.business.discovery_campaign_id).campaign_prompt)


class GenerationTests(PromptCase):
    def ready_prospect(self, campaign=None):
        email = make_email(campaign=campaign)
        prospect = email.prospect
        email.delete()  # a ready prospect without an email
        return prospect

    def test_campaign_default_email_prompt_is_used_and_recorded(self):
        campaign = make_campaign()
        _, first, _ = self.prompt_set(campaign=campaign)
        prospect = self.ready_prospect(campaign)
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.llm.system_prompts[-1], "CAMPAIGN RULES Dental\n\nFIRST TOUCH Dental")
        self.assertEqual(Email.objects.get(prospect=prospect).email_prompt, first)

    def test_chosen_email_prompt_wins(self):
        campaign = make_campaign()
        _, _, follow = self.prompt_set(campaign=campaign)
        prospect = self.ready_prospect(campaign)
        result = self.generate("--email-prompt-id", follow.pk)
        self.assertIn("Using email prompt: Dental / Follow-up", result.stdout)
        self.assertEqual(self.llm.system_prompts[-1], "CAMPAIGN RULES Dental\n\nFOLLOW UP Dental")
        self.assertEqual(Email.objects.get(prospect=prospect).email_prompt, follow)

    def test_campaign_without_a_prompt_uses_the_built_in_one(self):
        prospect = self.ready_prospect()
        self.generate()
        self.assertEqual(self.llm.system_prompts[-1], g.SYSTEM_PROMPT)
        self.assertIsNone(Email.objects.get(prospect=prospect).email_prompt)

    def test_campaign_prompt_without_email_prompts_adds_the_built_in_email_part(self):
        campaign = make_campaign()
        campaign.campaign_prompt = CampaignPrompt.objects.create(name="Bare", prompt="BARE RULES")
        campaign.save()
        self.ready_prospect(campaign)
        self.generate()
        self.assertEqual(self.llm.system_prompts[-1], g.compose_prompt("BARE RULES", g.DEFAULT_EMAIL_PROMPT))

    def test_each_campaign_uses_its_own_prompt(self):
        first_campaign, second_campaign = make_campaign(name="One"), make_campaign(name="Two")
        self.prompt_set("One", first_campaign)
        self.prompt_set("Two", second_campaign)
        self.ready_prospect(first_campaign)
        self.ready_prospect(second_campaign)
        self.generate()
        self.assertEqual(sorted(self.llm.system_prompts),
                         ["CAMPAIGN RULES One\n\nFIRST TOUCH One", "CAMPAIGN RULES Two\n\nFIRST TOUCH Two"])

    def test_unknown_email_prompt_is_refused(self):
        result = self.generate("--email-prompt-id", 999999)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Email prompt 999999 does not exist", result.stderr)

    def test_regenerate_keeps_the_prompt_or_uses_the_chosen_one(self):
        email = make_email()
        _, first, follow = self.prompt_set(campaign=email.business.discovery_campaign)
        Email.objects.filter(pk=email.pk).update(email_prompt=follow)
        url = reverse("admin:pipeline_email_regenerate", args=[email.pk])
        self.client.post(url)
        self.assertEqual(self.llm.system_prompts[-1], "CAMPAIGN RULES Dental\n\nFOLLOW UP Dental")
        self.client.post(url, {"email_prompt": first.pk})
        self.assertEqual(self.llm.system_prompts[-1], "CAMPAIGN RULES Dental\n\nFIRST TOUCH Dental")
        self.assertEqual(Email.objects.get(pk=email.pk).email_prompt, first)
        page = self.client.get(reverse("admin:pipeline_email_change", args=[email.pk])).content.decode()
        self.assertIn('name="email_prompt"', page)
        self.assertIn("Same prompt (First touch)", page)


class CampaignAdminTests(PromptCase):
    def test_status_is_read_only(self):
        url = reverse("admin:pipeline_discoverycampaign_add")
        form = self.client.get(url).context["adminform"].form
        self.assertNotIn("status", form.fields)
        self.client.post(url, {"name": "New", "status": "completed", "target_locations": "", "search_terms": ""})
        self.assertEqual(DiscoveryCampaign.objects.get(name="New").status, "pending")
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change",
                                       args=[DiscoveryCampaign.objects.get(name="New").pk])).content.decode()
        self.assertNotIn('name="status"', page)

    def test_select_or_create_a_prompt(self):
        free, _, _ = self.prompt_set("Free")
        taken, _, _ = self.prompt_set("Taken", campaign=make_campaign(name="Other"))
        url = reverse("admin:pipeline_discoverycampaign_add")
        page = self.client.get(url)
        choices = list(page.context["adminform"].form.fields["campaign_prompt"].queryset)
        self.assertIn(free, choices)
        self.assertNotIn(taken, choices)
        self.assertIn(reverse("admin:pipeline_campaignprompt_add"), page.content.decode())  # the + (create) link
        self.client.post(url, {"name": "Uses free", "campaign_prompt": free.pk})
        self.assertEqual(DiscoveryCampaign.objects.get(name="Uses free").campaign_prompt, free)
        response = self.client.post(url, {"name": "Steals", "campaign_prompt": taken.pk})
        self.assertFalse(DiscoveryCampaign.objects.filter(name="Steals").exists())
        self.assertContains(response, "Select a valid choice")

    def test_new_campaign_prompt_starts_from_the_built_in_text_and_gets_an_email_prompt(self):
        url = reverse("admin:pipeline_campaignprompt_add")
        self.assertEqual(self.client.get(url).context["adminform"].form.initial["prompt"], g.DEFAULT_CAMPAIGN_PROMPT)
        data = {"name": "Gyms", "prompt": "We sell to gyms.", "email_prompts-TOTAL_FORMS": "0",
                "email_prompts-INITIAL_FORMS": "0", "email_prompts-MIN_NUM_FORMS": "0", "email_prompts-MAX_NUM_FORMS": "1000"}
        self.client.post(url, data)
        created = CampaignPrompt.objects.get(name="Gyms")
        self.assertEqual(created.default_email_prompt().prompt, g.DEFAULT_EMAIL_PROMPT)

    def test_prompt_pages(self):
        campaign_prompt, first, _ = self.prompt_set(campaign=make_campaign(name="Mine"))
        for url in (reverse("admin:pipeline_campaignprompt_changelist"),
                    reverse("admin:pipeline_campaignprompt_change", args=[campaign_prompt.pk]),
                    reverse("admin:pipeline_emailprompt_changelist"),
                    reverse("admin:pipeline_emailprompt_change", args=[first.pk]),
                    reverse("admin:pipeline_emailprompt_add")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        detail = self.client.get(reverse("admin:pipeline_emailprompt_change", args=[first.pk])).content.decode()
        self.assertIn("CAMPAIGN RULES Dental", detail)  # the full prompt preview


class GeneratePageTests(PromptCase):
    def test_actions_open_the_generate_page_and_it_starts_a_run(self):
        campaign = make_campaign()
        _, _, follow = self.prompt_set(campaign=campaign)
        business = make_business(campaign, "Clinic")
        response = self.client.post(reverse("admin:pipeline_business_changelist"),
                                    {"action": "generate_emails", "_selected_action": [business.pk]})
        self.assertEqual(response["Location"], f"{reverse('admin:pipeline_prospect_generate')}?scope=business&ids={business.pk}")
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("1 selected business(es): Clinic", page)
        self.assertIn(reverse("admin:pipeline_emailprompt_add"), page)  # create a prompt from here
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("admin:pipeline_prospect_generate"), {
                "scope": "business", "ids": str(business.pk), "email_prompt": follow.pk, "regenerate": "on",
                "limit": "3"})
        run = PipelineRun.objects.get()
        self.assertEqual(run.arguments, ["--business-id", str(business.pk), "--email-prompt-id", str(follow.pk),
                                         "--regenerate", "--limit", "3"])
        self.assertRedirects(response, reverse("admin:pipeline_pipelinerun_change", args=[run.pk]),
                             fetch_redirect_response=False)

    def test_without_selection_it_covers_every_ready_prospect(self):
        page = self.client.get(reverse("admin:pipeline_prospect_generate")).content.decode()
        self.assertIn("every ready prospect", page)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:pipeline_prospect_generate"), {"dry_run": "on"})
        self.assertEqual(PipelineRun.objects.get().arguments, ["--dry-run"])

    def test_regenerate_action_ticks_regenerate(self):
        email = make_email()
        response = self.client.post(reverse("admin:pipeline_prospect_changelist"),
                                    {"action": "regenerate_emails", "_selected_action": [email.prospect_id]})
        form = self.client.get(response["Location"]).context["form"]
        self.assertTrue(form.initial["regenerate"])


class BusinessFilterTests(PromptCase):
    def test_filters(self):
        campaign = make_campaign()
        make_business(campaign, "High", score=85, google_rating=4.8, google_review_count=150, category="Dentist")
        make_business(campaign, "Mid", score=60, google_rating=4.1, google_review_count=30)
        make_business(campaign, "Unresearched")
        sent = make_email("sent", campaign=campaign)  # "Clinic N", score 80, with a contact and a prospect
        url = reverse("admin:pipeline_business_changelist")

        def names(**params):
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200, params)
            return {b.name for b in response.context["cl"].result_list}

        self.assertEqual(names(score="80"), {"High", sent.business.name})
        self.assertEqual(names(score="50"), {"Mid"})
        self.assertEqual(names(research="none"), {"Unresearched"})
        self.assertEqual(names(rating="45"), {"High"})
        self.assertEqual(names(reviews="20"), {"Mid"})
        self.assertEqual(names(contacts="yes"), {sent.business.name})
        self.assertEqual(names(emails="sent"), {sent.business.name})
        self.assertEqual(names(prospects="ready"), {sent.business.name})
        self.assertEqual(names(prospects="none"), {"High", "Mid", "Unresearched"})
        self.assertEqual(names(discovery="todo", score="80"), {"High", sent.business.name})
        self.assertEqual(names(category="Dentist"), {"High"})
        self.assertIn("qualification score", self.client.get(url).content.decode().lower())


@override_settings(AUTH_TOKEN=KEY)
class PromptApiTests(PromptCase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.credentials(HTTP_AUTH=KEY)

    def test_crud_and_campaign_link(self):
        response = self.api.post("/api/campaign-prompts/", {"name": "Spas", "prompt": "We sell to spas."}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        created = response.json()
        self.assertEqual([p["name"] for p in created["email_prompts"]], ["First-touch email"])
        follow = self.api.post("/api/email-prompts/", {"campaign_prompt": created["id"], "name": "Follow-up",
                                                        "prompt": "Short follow-up."}, format="json").json()
        self.assertEqual(follow["full_prompt"], "We sell to spas.\n\nShort follow-up.")
        campaign = self.api.post("/api/campaigns/", {"name": "Spa campaign", "campaign_prompt": created["id"]},
                                 format="json")
        self.assertEqual(campaign.status_code, 201, campaign.content)
        self.assertEqual(self.api.get(f"/api/campaign-prompts/{created['id']}/").json()["campaign"], campaign.json()["id"])
        second = self.api.post("/api/campaigns/", {"name": "Another", "campaign_prompt": created["id"]}, format="json")
        self.assertEqual(second.status_code, 400)  # one campaign per prompt
        status_change = self.api.patch(f"/api/campaigns/{campaign.json()['id']}/", {"status": "completed"}, format="json")
        self.assertEqual(status_change.json()["status"], "pending")  # read-only

    def test_regenerate_with_a_prompt_and_generate_run_option(self):
        email = make_email()
        _, _, follow = self.prompt_set(campaign=email.business.discovery_campaign)
        response = self.api.post(f"/api/emails/{email.pk}/regenerate/", {"email_prompt": follow.pk}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["email"]["email_prompt"], follow.pk)
        with mock.patch("pipeline.jobs.launch"), self.captureOnCommitCallbacks(execute=True):
            run = self.api.post("/api/runs/", {"command": "generate_emails", "options": {"email_prompt_id": follow.pk}},
                                format="json")
        self.assertEqual(run.json()["arguments"], ["--email-prompt-id", str(follow.pk)])


class BusinessQuickFilterTests(PromptCase):
    def test_quick_filters_search_and_nav_box(self):
        campaign = make_campaign()
        make_business(campaign, "Top", score=85, google_review_count=150, city="Karachi", category="Dental clinic")
        make_business(campaign, "Ok", score=55, google_review_count=10, city="Lahore", category="Dentist")
        make_business(campaign, "New", city="Lahore")
        url = reverse("admin:pipeline_business_changelist")
        page = self.client.get(url)
        chips = {chip["label"]: chip for chip in page.context["quick_filters"]}
        self.assertEqual(chips["All businesses"]["count"], 3)
        self.assertEqual(chips["Qualified (score 50+)"]["count"], 2)
        self.assertEqual(chips["Score 80+"]["count"], 1)
        self.assertEqual(chips["100+ reviews"]["count"], 1)
        self.assertEqual(chips["Not researched"]["count"], 1)
        self.assertTrue(chips["All businesses"]["active"])
        content = page.content.decode()
        self.assertIn("Jump to a page", content)
        self.assertNotIn("Start typing to filter", content)

        def names(**params):
            return {b.name for b in self.client.get(url, params).context["cl"].result_list}

        self.assertEqual(names(score="50up"), {"Top", "Ok"})
        self.assertTrue(self.client.get(url, {"score": "50up"}).context["quick_filters"][2]["active"])
        self.assertEqual(names(q="lahore"), {"Ok", "New"})
        self.assertEqual(names(q="dental clinic"), {"Top"})
        self.assertEqual(names(q="dentist", city="Lahore"), {"Ok"})
