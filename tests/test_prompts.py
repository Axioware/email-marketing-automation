"""Campaign prompts and email prompts: storage rules, how emails pick them, the admin pages and the API."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from datetime import time as dt_time
from rest_framework.test import APIClient

from pipeline.models import CampaignPrompt, DiscoveryCampaign, Email, EmailPrompt, PipelineRun
from pipeline.migrations import _prompt_texts as prompt_texts
from pipeline.services import research
from tests.support import CAMPAIGN_INLINE, make_business, make_campaign, make_email, review_env, run_command
from tests.test_generate_emails import FakeLLM

KEY = "prompt-test-key"


class PromptCase(TestCase):
    def setUp(self):
        self.llm = FakeLLM()
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(os.environ, review_env(self.llm))
        env.start()
        self.addCleanup(env.stop)
        self.user = get_user_model().objects.create_superuser("admin", "a@example.org", "pw")
        self.client.force_login(self.user)

    def prompt_set(self, name="Dental", campaign=None):
        """A campaign prompt, an initial-email prompt and a second initial prompt the campaign does not use."""
        campaign_prompt = CampaignPrompt.objects.create(name=name, prompt=f"CAMPAIGN RULES {name}")
        first = EmailPrompt.objects.create(name=f"First touch {name}", prompt=f"FIRST TOUCH {name}")
        other = EmailPrompt.objects.create(name=f"Other {name}", prompt=f"OTHER {name}")
        if campaign is not None:
            campaign.campaign_prompt = campaign_prompt
            campaign.save()
            campaign.initial_email_prompts.set([first])
        return campaign_prompt, first, other

    def generate(self, *args):
        return run_command("generate_emails", *args)


class ModelTests(PromptCase):
    def test_seeded_axioware_prompts(self):
        """The migrations leave the qualification criteria in the campaign prompt and the whole email text (what the
        model received before 0008) in the initial-email prompt."""
        seeded = CampaignPrompt.objects.get(name="Axioware - dental clinics")
        self.assertEqual(seeded.prompt, prompt_texts.QUALIFICATION_PROMPT)
        self.assertFalse(hasattr(research, "AGENT_INSTRUCTIONS"))  # nothing is added in front of it
        first_touch = EmailPrompt.objects.get(name="First-touch email")
        self.assertEqual(first_touch.kind, "initial")
        self.assertEqual(first_touch.prompt,
                         f"{prompt_texts.OLD_CAMPAIGN_PROMPT.strip()}\n\n{prompt_texts.OLD_EMAIL_PROMPT.strip()}")

    def test_prompts_are_shared_between_campaigns(self):
        campaign_prompt, first, _ = self.prompt_set(campaign=make_campaign(name="A"))
        second = DiscoveryCampaign.objects.create(name="B", campaign_prompt=campaign_prompt)
        second.initial_email_prompts.set([first])
        self.assertEqual(set(campaign_prompt.campaigns.all()), {second, DiscoveryCampaign.objects.get(name="A")})
        self.assertEqual(first.initial_for_campaigns.count(), 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailPrompt.objects.create(name=first.name, prompt="x")  # names are unique

    def test_deleting_a_prompt_keeps_campaigns_and_emails(self):
        email = make_email()
        campaign = email.business.discovery_campaign
        campaign_prompt, first, _ = self.prompt_set(campaign=campaign)
        Email.objects.filter(pk=email.pk).update(email_prompt=first)
        campaign_prompt.delete()
        first.delete()
        email.refresh_from_db()
        campaign.refresh_from_db()
        self.assertIsNone(email.email_prompt)
        self.assertIsNone(campaign.campaign_prompt)
        self.assertFalse(campaign.initial_email_prompts.exists())

    def test_campaign_settings_validation(self):
        from django.core.exceptions import ValidationError

        campaign = make_campaign()
        for values, field in (({"min_score": 101}, "min_score"),
                              ({"send_window_start": dt_time(18), "send_window_end": dt_time(9)}, "send_window_end"),
                              ({"send_window_start": dt_time(9)}, "send_window_end"),
                              ({"send_timezone": "Mars/Base"}, "send_timezone")):
            fresh = DiscoveryCampaign.objects.get(pk=campaign.pk)
            for key, value in values.items():
                setattr(fresh, key, value)
            with self.assertRaises(ValidationError, msg=values) as caught:
                fresh.clean()
            self.assertIn(field, caught.exception.message_dict)


class GenerationTests(PromptCase):
    def ready_prospect(self, campaign=None):
        email = make_email(campaign=campaign)
        prospect = email.prospect
        email.delete()  # a ready prospect without an email
        return prospect

    def test_the_campaigns_initial_prompt_is_used_and_recorded(self):
        campaign = make_campaign()
        _, first, _ = self.prompt_set(campaign=campaign)
        prospect = self.ready_prospect(campaign)
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.llm.system_prompts[-1], "FIRST TOUCH Dental")  # the email prompt alone
        self.assertEqual(Email.objects.get(prospect=prospect).email_prompt, first)
        prospect.refresh_from_db()
        self.assertEqual(prospect.initial_email_prompt, first)

    def test_chosen_email_prompt_wins(self):
        campaign = make_campaign()
        _, _, other = self.prompt_set(campaign=campaign)
        prospect = self.ready_prospect(campaign)
        result = self.generate("--email-prompt-id", other.pk)
        self.assertIn("Using email prompt: Other Dental", result.stdout)
        self.assertEqual(self.llm.system_prompts[-1], "OTHER Dental")
        self.assertEqual(Email.objects.get(prospect=prospect).email_prompt, other)

    def test_campaign_without_initial_prompts_is_skipped(self):
        prospect = self.ready_prospect(make_campaign(initial_prompts=[]))
        result = self.generate()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"Prospect {prospect.pk} <{prospect.email}> skipped: its campaign has no initial-email prompt",
                      result.stdout)
        self.assertIn("1 skipped (no email prompt)", result.stdout)
        self.assertEqual(self.llm.system_prompts, [])
        self.assertFalse(Email.objects.filter(prospect=prospect).exists())
        prospect.refresh_from_db()
        self.assertIsNone(prospect.initial_email_prompt)  # no A/B group until it can get an email
        email = make_email(campaign=prospect.business.discovery_campaign)  # regenerating says why too
        response = self.client.post(reverse("admin:pipeline_email_regenerate", args=[email.pk]), follow=True)
        self.assertContains(response, "Cannot regenerate: its campaign has no initial-email prompt")

    def test_campaign_prompt_is_not_needed_to_write_emails(self):
        prospect = self.ready_prospect(make_campaign(campaign_prompt=None))
        self.assertEqual(self.generate().returncode, 0)
        self.assertTrue(Email.objects.filter(prospect=prospect).exists())

    def test_each_campaign_uses_its_own_prompt(self):
        first_campaign, second_campaign = make_campaign(name="One"), make_campaign(name="Two")
        self.prompt_set("One", first_campaign)
        self.prompt_set("Two", second_campaign)
        self.ready_prospect(first_campaign)
        self.ready_prospect(second_campaign)
        self.generate()
        self.assertEqual(sorted(self.llm.system_prompts), ["FIRST TOUCH One", "FIRST TOUCH Two"])

    def test_unknown_email_prompt_is_refused(self):
        result = self.generate("--email-prompt-id", 999999)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Email prompt 999999 does not exist", result.stderr)

    def test_regenerate_keeps_the_prompt_or_uses_the_chosen_one(self):
        email = make_email()
        _, first, other = self.prompt_set(campaign=email.business.discovery_campaign)
        Email.objects.filter(pk=email.pk).update(email_prompt=other)
        url = reverse("admin:pipeline_email_regenerate", args=[email.pk])
        self.client.post(url)
        self.assertEqual(self.llm.system_prompts[-1], "OTHER Dental")
        self.client.post(url, {"email_prompt": first.pk})
        self.assertEqual(self.llm.system_prompts[-1], "FIRST TOUCH Dental")
        self.assertEqual(Email.objects.get(pk=email.pk).email_prompt, first)
        page = self.client.get(reverse("admin:pipeline_email_change", args=[email.pk])).content.decode()
        self.assertIn('name="email_prompt"', page)
        self.assertIn("Same prompt (First touch Dental)", page)


class CampaignAdminTests(PromptCase):
    def test_status_is_read_only(self):
        url = reverse("admin:pipeline_discoverycampaign_add")
        form = self.client.get(url).context["adminform"].form
        self.assertNotIn("status", form.fields)
        self.client.post(url, {**CAMPAIGN_INLINE, "name": "New", "status": "completed", "target_locations": "", "search_terms": ""})
        self.assertEqual(DiscoveryCampaign.objects.get(name="New").status, "pending")
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change",
                                       args=[DiscoveryCampaign.objects.get(name="New").pk])).content.decode()
        self.assertNotIn('name="status"', page)

    def test_choose_prompts_and_settings_on_the_campaign(self):
        campaign_prompt, first, other = self.prompt_set("Shared", campaign=make_campaign(name="Other"))
        follow = EmailPrompt.objects.create(name="A follow-up", kind="follow_up", prompt="F")
        url = reverse("admin:pipeline_discoverycampaign_add")
        page = self.client.get(url)
        form = page.context["adminform"].form
        self.assertIn(campaign_prompt, list(form.fields["campaign_prompt"].queryset))  # shared prompts are offered
        self.assertEqual(set(form.fields["initial_email_prompts"].queryset), {first, other} | set(
            EmailPrompt.objects.filter(kind="initial")))
        self.assertNotIn(follow, list(form.fields["initial_email_prompts"].queryset))
        self.assertIn(reverse("admin:pipeline_campaignprompt_add"), page.content.decode())  # the + (create) link
        response = self.client.post(url, {**CAMPAIGN_INLINE, "name": "A/B", "campaign_prompt": campaign_prompt.pk,
                                          "initial_email_prompts": [first.pk, other.pk], "min_score": "65",
                                          "send_window_start": "09:00", "send_window_end": "18:00",
                                          "send_timezone": "Asia/Karachi"})
        self.assertEqual(response.status_code, 302, response.content[:300])
        created = DiscoveryCampaign.objects.get(name="A/B")
        self.assertEqual(created.campaign_prompt, campaign_prompt)
        self.assertEqual(set(created.initial_email_prompts.all()), {first, other})
        self.assertEqual((created.min_score, str(created.send_window_end)), (65, "18:00:00"))
        bad = self.client.post(url, {**CAMPAIGN_INLINE, "name": "Bad", "send_window_start": "18:00",
                                     "send_window_end": "09:00"})
        self.assertContains(bad, "The end must be later than the start")
        self.assertNotIn("daily_cap", self.client.get(url).content.decode())  # global: the Settings page
        defaults = self.client.post(url, {**CAMPAIGN_INLINE, "name": "Defaults"})
        self.assertEqual(defaults.status_code, 302)
        made = DiscoveryCampaign.objects.get(name="Defaults")
        self.assertEqual((made.min_score, made.send_timezone), (50, "Asia/Karachi"))

    def test_sending_settings_validation(self):
        from django.core.exceptions import ValidationError

        from pipeline.models import SendingSettings

        current = SendingSettings.current()
        self.assertEqual((current.daily_cap, current.gap_fixed_seconds, current.gap_random_min_seconds,
                          current.gap_random_max_seconds), (40, 120, 180, 300))
        for values, field in (({"gap_random_min_seconds": 400, "gap_random_max_seconds": 300}, "gap_random_max_seconds"),
                              ({"daily_cap": 0}, "daily_cap")):
            row = SendingSettings.current()
            for key, value in values.items():
                setattr(row, key, value)
            with self.assertRaises(ValidationError, msg=values) as caught:
                row.clean()
            self.assertIn(field, caught.exception.message_dict)
        SendingSettings(daily_cap=10).save()  # saving another one keeps the single row
        self.assertEqual((SendingSettings.objects.count(), SendingSettings.current().daily_cap), (1, 10))

    def test_new_prompts_start_empty(self):
        self.assertNotIn("prompt", self.client.get(reverse("admin:pipeline_campaignprompt_add")).context["adminform"]
                         .form.initial)  # nothing built in
        self.assertNotIn("prompt", self.client.get(reverse("admin:pipeline_emailprompt_add")).context["adminform"]
                         .form.initial)
        response = self.client.post(reverse("admin:pipeline_campaignprompt_add"),
                                    {"name": "Gyms", "prompt": "Gyms with 200+ members score high."})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CampaignPrompt.objects.get(name="Gyms").prompt, "Gyms with 200+ members score high.")

    def test_campaign_page_warns_about_missing_prompts(self):
        bare = make_campaign(name="Bare", campaign_prompt=None, initial_prompts=[])
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change", args=[bare.pk]))
        self.assertContains(page, "This campaign has no campaign prompt: its businesses will not be researched")
        self.assertContains(page, "This campaign has no initial-email prompt")
        fine = make_campaign(name="Fine")
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change", args=[fine.pk]))
        self.assertNotContains(page, "has no campaign prompt")
        self.assertNotContains(page, "no initial-email prompt")

    def test_prompt_pages(self):
        campaign_prompt, first, _ = self.prompt_set(campaign=make_campaign(name="Mine"))
        for url in (reverse("admin:pipeline_campaignprompt_changelist"),
                    reverse("admin:pipeline_campaignprompt_change", args=[campaign_prompt.pk]),
                    reverse("admin:pipeline_emailprompt_changelist"),
                    reverse("admin:pipeline_emailprompt_change", args=[first.pk]),
                    reverse("admin:pipeline_emailprompt_add")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        detail = self.client.get(reverse("admin:pipeline_campaignprompt_change", args=[campaign_prompt.pk])).content.decode()
        self.assertIn("Answer format (the app&#x27;s fields plus your output fields)", detail)
        self.assertIn("SCRAPE: read one more page", detail)
        self.assertIn("team_size | integer | How many staff", detail)  # the default output fields, editable
        self.assertIn("Mine", detail)  # used by campaigns
        detail = self.client.get(reverse("admin:pipeline_emailprompt_change", args=[first.pk])).content.decode()
        self.assertNotIn("CAMPAIGN RULES Dental", detail)  # the email prompt is used on its own
        self.assertIn("initial email", detail)


class OutputFieldTests(PromptCase):
    def test_edit_output_fields_on_the_prompt_page(self):
        campaign_prompt, _, _ = self.prompt_set(campaign=make_campaign(name="Mine"))
        url = reverse("admin:pipeline_campaignprompt_change", args=[campaign_prompt.pk])
        text = ("team_size | integer | Staff the site states.\n"
                "existing_call_tool | text | The AI receptionist or call service the site shows.\n"
                "\n"
                "offers_emergency_care | yes/no | Whether the clinic offers emergency appointments.")
        response = self.client.post(url, {"name": campaign_prompt.name, "prompt": campaign_prompt.prompt,
                                          "output_fields_text": text})
        self.assertEqual(response.status_code, 302, response.content[:500])
        campaign_prompt.refresh_from_db()
        self.assertEqual([(f["name"], f["type"]) for f in campaign_prompt.output_fields],
                         [("team_size", "integer"), ("existing_call_tool", "text"), ("offers_emergency_care", "yes/no")])
        page = self.client.get(url).content.decode()
        self.assertIn("existing_call_tool", page)  # in the editor and in the answer format the AI receives
        self.assertIn("SCORE only: The AI receptionist or call service the site shows.", page)
        for bad, message in (("score | integer | x", "one of the app&#x27;s own fields"),
                             ("Team Size | integer | x", "must start with a lowercase letter"),
                             ("a | date | x", "the type must be one of"),
                             ("a | text", "name | type | description"),
                             ("a | text | x\na | integer | y", "listed twice")):
            response = self.client.post(url, {"name": campaign_prompt.name, "prompt": campaign_prompt.prompt,
                                              "output_fields_text": bad})
            self.assertContains(response, message, msg_prefix=bad)

    @override_settings(AUTH_TOKEN="t" * 20)
    def test_output_fields_over_the_api(self):
        from rest_framework.test import APIClient

        api = APIClient()
        api.credentials(HTTP_AUTH="t" * 20)
        created = api.post("/api/campaign-prompts/", {"name": "API", "prompt": "Score it.", "output_fields": [
            {"name": "has_parking", "type": "yes/no", "description": "Whether parking is mentioned."}]}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["output_fields"][0]["name"], "has_parking")
        bad = api.post("/api/campaign-prompts/", {"name": "Bad", "prompt": "x", "output_fields": [
            {"name": "url", "type": "text", "description": "x"}]}, format="json")
        self.assertEqual(bad.status_code, 400)
        default = api.post("/api/campaign-prompts/", {"name": "Default", "prompt": "x"}, format="json").json()
        self.assertEqual([f["name"] for f in default["output_fields"]], ["team_size", "languages"])


class GeneratePageTests(PromptCase):
    def test_actions_open_the_generate_page_and_it_starts_a_run(self):
        campaign = make_campaign()
        _, _, follow = self.prompt_set(campaign=campaign)  # "follow": a second initial prompt
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
        response = self.api.post("/api/campaign-prompts/", {"name": "Spas", "prompt": "Day spas score high."}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        created = response.json()
        self.assertEqual((created["prompt"], created["campaigns"]), ("Day spas score high.", []))
        self.assertNotIn("full_prompt", created)
        first = self.api.post("/api/email-prompts/", {"name": "First", "kind": "initial", "prompt": "Write to spas."},
                              format="json")
        self.assertEqual(first.status_code, 201, first.content)
        follow = self.api.post("/api/email-prompts/", {"name": "Follow-up", "kind": "follow_up",
                                                        "prompt": "Short follow-up."}, format="json")
        self.assertEqual(follow.status_code, 201, follow.content)
        duplicate = self.api.post("/api/email-prompts/", {"name": "First", "prompt": "x"}, format="json")
        self.assertEqual(duplicate.status_code, 400)  # names are unique
        self.assertEqual(self.api.post("/api/email-prompts/", {"name": "Odd", "kind": "other", "prompt": "x"},
                                       format="json").status_code, 400)
        campaign = self.api.post("/api/campaigns/", {
            "name": "Spa campaign", "campaign_prompt": created["id"], "initial_email_prompts": [first.json()["id"]],
            "min_score": 70, "send_window_start": "09:00", "send_window_end": "18:00"}, format="json")
        self.assertEqual(campaign.status_code, 201, campaign.content)
        self.assertEqual((campaign.json()["min_score"], campaign.json()["send_timezone"]), (70, "Asia/Karachi"))
        self.assertEqual(self.api.get(f"/api/campaign-prompts/{created['id']}/").json()["campaigns"],
                         [campaign.json()["id"]])
        shared = self.api.post("/api/campaigns/", {"name": "Another", "campaign_prompt": created["id"]}, format="json")
        self.assertEqual(shared.status_code, 201)  # a campaign prompt can be shared
        wrong = self.api.post("/api/campaigns/", {"name": "Wrong", "initial_email_prompts": [follow.json()["id"]]},
                              format="json")
        self.assertEqual(wrong.status_code, 400)  # a follow-up prompt cannot write the initial email
        bad_window = self.api.patch(f"/api/campaigns/{campaign.json()['id']}/", {"send_window_end": "08:00"},
                                    format="json")
        self.assertEqual(bad_window.status_code, 400)
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
        self.assertEqual(chips["Qualified"]["count"], 2)  # the campaign's minimum score: 50
        self.assertEqual(chips["Needs your review"]["count"], 1)  # "New" has no website
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
        self.assertEqual(names(qualification="qualified"), {"Top", "Ok"})
        self.assertEqual(names(qualification="review"), {"New"})
        active = {chip["label"]: chip["active"]
                  for chip in self.client.get(url, {"qualification": "qualified"}).context["quick_filters"]}
        self.assertTrue(active["Qualified"])
        self.assertEqual(names(q="lahore"), {"Ok", "New"})
        self.assertEqual(names(q="dental clinic"), {"Top"})
        self.assertEqual(names(q="dentist", city="Lahore"), {"Ok"})
