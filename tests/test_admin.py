"""The Django admin: every model's pages, the email review workflow and the pipeline-run actions."""
import os
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from pipeline.models import (
    Business,
    BusinessContact,
    BusinessSource,
    BusinessWebsiteProfile,
    DiscoveryCampaign,
    Email,
    EmailOpenEvent,
    PipelineRun,
    Prospect,
)
from pipeline.services import generation as g
from tests.fake_smtp import FakeSmtpServer
from tests.support import BASE, make_business, make_campaign, make_contact, make_email, review_env
from tests.test_generate_emails import FakeLLM


class AdminCase(TestCase):
    def setUp(self):
        self.smtp = FakeSmtpServer()
        self.llm = FakeLLM()
        self.addCleanup(self.smtp.close)
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(os.environ, review_env(self.smtp, self.llm))
        env.start()
        self.addCleanup(env.stop)
        self.user = get_user_model().objects.create_superuser("admin", "admin@example.org", "pw")
        self.client.force_login(self.user)

    def email(self, *args, **kwargs):
        return make_email(*args, **kwargs).pk

    def row(self, email_id):
        return Email.objects.filter(pk=email_id).values().get()

    def change_url(self, email_id):
        return reverse("admin:pipeline_email_change", args=[email_id])

    def post_action(self, email_id, action, follow=True, **data):
        return self.client.post(reverse(f"admin:pipeline_email_{action}", args=[email_id]), data, follow=follow)

    def edit(self, email_id, subject, body):
        page = self.client.get(self.change_url(email_id))
        data = {"subject": subject, "body": body}
        for inline in page.context["inline_admin_formsets"]:
            formset = inline.formset
            for key, value in formset.management_form.initial.items():
                data[f"{formset.prefix}-{key}"] = value
        return self.client.post(self.change_url(email_id), data, follow=True)


class AdminPagesTests(AdminCase):
    def test_every_model_has_working_list_and_detail_pages(self):
        email = make_email("sent")
        EmailOpenEvent.objects.create(email=email, user_agent="Mail")
        BusinessSource.objects.create(business=email.business, source="google_maps", raw_data={"name": "x"})
        run = PipelineRun.objects.create(command="send_emails", arguments=[], status="succeeded", output="done")
        objects = [email.business.discovery_campaign, email.business, email.business.sources.first(),
                   email.business.website_profile, email.contact, email.prospect, email, email.open_events.first(), run]
        for obj in objects:
            name = obj._meta.model_name
            changelist = self.client.get(reverse(f"admin:pipeline_{name}_changelist"))
            self.assertEqual(changelist.status_code, 200, name)
            detail = self.client.get(reverse(f"admin:pipeline_{name}_change", args=[obj.pk]))
            self.assertEqual(detail.status_code, 200, name)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_admin_requires_staff(self):
        self.client.logout()
        response = self.client.get(reverse("admin:pipeline_email_changelist"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])
        email = make_email()
        self.post_action(email.pk, "approve", follow=False)
        self.assertEqual(self.row(email.pk)["status"], "in_review")

    def test_campaign_form_takes_comma_separated_lists(self):
        response = self.client.post(reverse("admin:pipeline_discoverycampaign_add"), {
            "name": "Karachi dentists", "target_country": "Pakistan", "target_locations": "Karachi, Clifton",
            "search_terms": "dental clinic, dentist", "status": "pending"}, follow=True)
        self.assertEqual(response.status_code, 200)
        campaign = DiscoveryCampaign.objects.get(name="Karachi dentists")
        self.assertEqual((campaign.target_locations, campaign.search_terms), (["Karachi", "Clifton"], ["dental clinic", "dentist"]))
        page = self.client.get(reverse("admin:pipeline_discoverycampaign_change", args=[campaign.pk])).content.decode()
        self.assertIn('value="Karachi, Clifton"', page)
        self.assertIn("Fetch businesses", page)

    def test_campaign_rejects_several_countries(self):
        response = self.client.post(reverse("admin:pipeline_discoverycampaign_add"), {
            "name": "x", "target_country": "Pakistan, India", "status": "pending"})
        self.assertContains(response, "Enter one country only")
        self.assertFalse(DiscoveryCampaign.objects.exists())

    def test_business_search_and_counts(self):
        campaign = make_campaign()
        business = make_business(campaign, "Smile Dental", score=77)
        make_contact(business, "a@smile.pk")
        make_business(campaign, "Other Place")
        page = self.client.get(reverse("admin:pipeline_business_changelist"), {"q": "smile"}).content.decode()
        self.assertIn("Smile Dental", page)
        self.assertNotIn("Other Place", page)
        self.assertIn(">77<", page)


class PipelineActionTests(AdminCase):
    def changelist_action(self, model, action, ids):
        return self.client.post(reverse(f"admin:pipeline_{model}_changelist"),
                                {"action": action, "_selected_action": ids})

    def test_business_actions_open_the_run_form_with_the_selected_businesses(self):
        campaign = make_campaign()
        a, b = make_business(campaign, "A"), make_business(campaign, "B")
        for action, command, flags in (("research_websites", "research_websites", "--redo"),
                                       ("find_stakeholders", "find_stakeholders", "--redo"),
                                       ("verify_emails", "verify_emails", "")):
            response = self.changelist_action("business", action, [a.pk, b.pk])
            self.assertEqual(response.status_code, 302, action)
            form = self.client.get(response["Location"])
            self.assertEqual(form.context["adminform"].form.initial["command"], command)
            arguments = form.context["adminform"].form.initial["arguments_text"]
            self.assertIn(f"--business-id {a.pk} --business-id {b.pk}", arguments)
            self.assertIn(flags, arguments)

    def test_starting_a_run_from_the_form(self):
        with mock.patch("pipeline.jobs.launch") as launch, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("admin:pipeline_pipelinerun_add"),
                                        {"command": "verify_emails", "arguments_text": "--limit 5 --dry-run"})
        run = PipelineRun.objects.get()
        self.assertRedirects(response, reverse("admin:pipeline_pipelinerun_change", args=[run.pk]),
                             fetch_redirect_response=False)
        self.assertEqual((run.command, run.arguments, run.status, run.created_by), ("verify_emails", ["--limit", "5", "--dry-run"], "queued", self.user))
        launch.assert_called_once_with(run.pk)
        page = self.client.get(reverse("admin:pipeline_pipelinerun_change", args=[run.pk])).content.decode()
        self.assertIn('http-equiv="refresh"', page)  # follows progress while queued/running
        self.assertIn("Stop this run", page)

    def test_invalid_arguments_are_refused_before_starting(self):
        with mock.patch("pipeline.jobs.launch") as launch:
            response = self.client.post(reverse("admin:pipeline_pipelinerun_add"),
                                        {"command": "verify_emails", "arguments_text": "--limit nope"})
        self.assertContains(response, "invalid int value")
        self.assertFalse(PipelineRun.objects.exists())
        launch.assert_not_called()

    def test_run_form_lists_every_command_and_option(self):
        page = self.client.get(reverse("admin:pipeline_pipelinerun_add")).content.decode()
        for flag in ("--campaign-id", "--retry-failed", "--min-score", "--probe-all", "--regenerate", "--send"):
            self.assertIn(flag, page)

    def test_finished_run_shows_output_and_run_again(self):
        run = PipelineRun.objects.create(command="send_emails", arguments=["--limit", "1"], status="succeeded",
                                         output="Preview done: <b>1</b>")
        page = self.client.get(reverse("admin:pipeline_pipelinerun_change", args=[run.pk])).content.decode()
        self.assertIn("Preview done: &lt;b&gt;1&lt;/b&gt;", page)
        self.assertIn("Run again", page)
        self.assertNotIn('http-equiv="refresh"', page)

    def test_cancel_a_queued_run(self):
        run = PipelineRun.objects.create(command="send_emails")
        self.client.post(reverse("admin:pipeline_pipelinerun_cancel", args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")

    def test_prospect_actions(self):
        email = make_email()
        response = self.changelist_action("prospect", "mark_do_not_contact", [email.prospect_id])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Prospect.objects.get().do_not_contact)
        response = self.changelist_action("prospect", "regenerate_emails", [email.prospect_id])
        self.assertEqual(response["Location"], f"{reverse('admin:pipeline_prospect_generate')}?scope=prospect"
                                               f"&ids={email.prospect_id}&regenerate=1")

    def test_send_selected_only_takes_approved_emails(self):
        approved, in_review = self.email("approved"), self.email()
        response = self.changelist_action("email", "send_selected", [approved, in_review])
        arguments = self.client.get(response["Location"]).context["adminform"].form.initial["arguments_text"]
        self.assertEqual(arguments, f"--send --email-id {approved}")


class EmailReviewTests(AdminCase):
    def test_list_shows_counts_open_rate_and_filters_by_status(self):
        self.email("in_review", subject="Review me")
        self.email("approved", subject="Ready to go")
        url = reverse("admin:pipeline_email_changelist")
        page = self.client.get(url, {"status__exact": "in_review"}).content.decode()
        self.assertIn("Review me", page)
        self.assertNotIn("Ready to go", page)
        self.assertIn("Open rate", page)
        both = self.client.get(url).content.decode()
        self.assertIn("Review me", both)
        self.assertIn("Ready to go", both)

    def test_search(self):
        self.email(subject="Alpha subject")
        self.email(subject="Beta subject")
        page = self.client.get(reverse("admin:pipeline_email_changelist"), {"q": "alpha"}).content.decode()
        self.assertIn("Alpha subject", page)
        self.assertNotIn("Beta subject", page)

    def test_detail_preview_never_uses_the_tracking_url(self):
        eid = self.email()
        token = self.row(eid)["tracking_token"]
        page = self.client.get(self.change_url(eid)).content.decode()
        self.assertNotIn(f"{BASE}/{token}", page)  # viewing in the admin must not count as an open
        self.assertIn("storage/v1/object/public/email-assets/footer.png", page)
        self.assertIn("Hours: 9-5", page)  # website findings shown to the reviewer
        self.assertIn("Strong reviews", page)

    def test_hostile_recipient_cannot_reach_inline_javascript(self):
        evil = "x');alert(document.cookie);//@evil.org"
        eid = self.email("approved", recipient=evil)
        page = self.client.get(self.change_url(eid)).content.decode()
        for handler in re.findall(r'\son\w+="([^"]*)"', page):
            self.assertNotIn("alert", handler)
            self.assertNotIn("evil.org", handler)

    def test_unknown_email_redirects_with_a_message(self):
        response = self.client.get(self.change_url(999999), follow=True)
        self.assertContains(response, "doesn’t exist")

    def test_model_or_db_text_is_escaped(self):
        eid = self.email(subject="<script>alert(1)</script>", body="Hi <b>there</b>")
        for url in (reverse("admin:pipeline_email_changelist"), self.change_url(eid)):
            page = self.client.get(url).content.decode()
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;", page)

    def test_edit_saves_content_and_keeps_the_tracking_token(self):
        eid = self.email()
        token = self.row(eid)["tracking_token"]
        page = self.edit(eid, "  New   subject ", "Hi Ali,\r\n\r\nRewritten.")
        self.assertContains(page, "Saved.")
        row = self.row(eid)
        self.assertEqual(row["subject"], "New subject")
        self.assertEqual(g.editable_body(row["body_text"]), "Hi Ali,\n\nRewritten.")
        self.assertIn(g.FOOTER_TEXT.splitlines()[0], row["body_text"])
        self.assertIn(f"{BASE}/{token}", row["body_html"])
        self.assertIn("Rewritten.", row["body_html"])
        self.assertIsNotNone(row["edited_at"])
        self.assertEqual(row["status"], "in_review")

    def test_editing_an_approved_email_sends_it_back_to_review(self):
        eid = self.email("approved")
        self.assertContains(self.edit(eid, "Changed", "Hi"), "went back to review")
        self.assertEqual((self.row(eid)["status"], self.row(eid)["reviewed_at"]), ("in_review", None))

    def test_sent_emails_cannot_be_edited(self):
        eid = self.email("sent")
        page = self.client.get(self.change_url(eid))
        self.assertNotIn("subject", page.context["adminform"].form.fields)
        self.client.post(self.change_url(eid), {"subject": "Changed", "body": "Hi"})
        self.assertEqual(self.row(eid)["subject"], "Hello there")

    def test_empty_or_oversized_edits_are_refused(self):
        eid = self.email()
        for subject, body in (("", "Hi"), ("S", "   "), ("x" * 300, "Hi"), ("S", "b" * 6000)):
            self.edit(eid, subject, body)
        self.assertEqual(self.row(eid)["subject"], "Hello there")

    def test_approve_moves_to_the_next_email_in_review(self):
        first, second = self.email(), self.email()
        response = self.post_action(first, "approve", follow=False)
        self.assertEqual(response["Location"], self.change_url(second))
        row = self.row(first)
        self.assertEqual(row["status"], "approved")
        self.assertIsNotNone(row["reviewed_at"])

    def test_placeholder_text_blocks_approval(self):
        eid = self.email(body="Hi PLACEHOLDER NAME")
        self.assertContains(self.post_action(eid, "approve"), "placeholder text")
        self.assertEqual(self.row(eid)["status"], "in_review")

    def test_reject_with_note_and_reopen(self):
        eid = self.email()
        self.post_action(eid, "reject", note="Too salesy")
        self.assertEqual((self.row(eid)["status"], self.row(eid)["review_note"]), ("rejected", "Too salesy"))
        self.assertContains(self.client.get(self.change_url(eid)), "Too salesy")
        self.post_action(eid, "reopen")
        self.assertEqual(self.row(eid)["status"], "in_review")

    def test_wrong_state_transitions_are_refused(self):
        sent = self.email("sent")
        for action in ("approve", "reject", "reopen", "regenerate"):
            self.post_action(sent, action)
        self.assertEqual(self.row(sent)["status"], "sent")

    def test_actions_need_post(self):
        eid = self.email()
        self.assertEqual(self.client.get(reverse("admin:pipeline_email_approve", args=[eid])).status_code, 405)
        self.assertEqual(self.row(eid)["status"], "in_review")

    def test_bulk_approve_and_reject(self):
        a, b, c = self.email(), self.email(body="PLACEHOLDER"), self.email()
        url = reverse("admin:pipeline_email_changelist")
        page = self.client.post(url, {"action": "approve_selected", "_selected_action": [a, b]}, follow=True)
        self.assertContains(page, "1 email(s) approved, 1 skipped")
        self.assertEqual([self.row(i)["status"] for i in (a, b, c)], ["approved", "in_review", "in_review"])
        self.client.post(url, {"action": "reject_selected", "_selected_action": [b, c]})
        self.assertEqual([self.row(i)["status"] for i in (b, c)], ["rejected", "rejected"])

    def test_regenerate_rewrites_with_the_llm(self):
        eid = self.email("rejected")
        token = self.row(eid)["tracking_token"]
        self.assertContains(self.post_action(eid, "regenerate"), "Regenerated with openai")
        row = self.row(eid)
        self.assertEqual(row["status"], "in_review")
        self.assertIn("Idea for Clinic", row["subject"])
        self.assertEqual(row["tracking_token"], token)
        self.assertIn(f"{BASE}/{token}", row["body_html"])

    def test_regenerate_failure_is_shown_and_changes_nothing(self):
        eid = self.email()
        self.llm.mode = "bad"
        self.assertContains(self.post_action(eid, "regenerate"), "Regeneration failed")
        self.assertEqual(self.row(eid)["subject"], "Hello there")

    def test_send_needs_the_typed_recipient_and_then_sends(self):
        eid = self.email("approved")
        recipient = self.row(eid)["recipient"]
        self.post_action(eid, "send", confirm="wrong@x.org")
        self.assertEqual((self.row(eid)["status"], self.smtp.messages), ("approved", []))
        self.assertContains(self.post_action(eid, "send", confirm=recipient.upper()), f"Sent to {recipient}")
        self.assertEqual(self.row(eid)["status"], "sent")
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(self.smtp.messages[0][1], [recipient])
        self.assertEqual(Prospect.objects.get().outreach_status, "contacted")

    def test_only_approved_emails_can_be_sent(self):
        eid = self.email("in_review")
        self.post_action(eid, "send", confirm=self.row(eid)["recipient"])
        self.assertEqual((self.row(eid)["status"], self.smtp.messages), ("in_review", []))

    def test_do_not_contact_prospect_is_never_sent(self):
        eid = self.email("approved", do_not_contact=True)
        self.assertContains(self.post_action(eid, "send", confirm=self.row(eid)["recipient"]), "no longer ready")
        self.assertEqual(self.smtp.messages, [])

    def test_missing_smtp_settings(self):
        eid = self.email("approved")
        with mock.patch.dict(os.environ, {"SMTP_PASSWORD": ""}):
            self.assertContains(self.client.get(self.change_url(eid)), "SMTP is not configured")
            page = self.post_action(eid, "send", confirm=self.row(eid)["recipient"])
        self.assertContains(page, "SMTP is not configured")
        self.assertEqual(self.row(eid)["status"], "approved")

    def test_smtp_rejection_marks_failed(self):
        eid = self.email("approved", recipient="reject@x.org")
        self.assertContains(self.post_action(eid, "send", confirm="reject@x.org"), "marked failed")
        self.assertEqual(self.row(eid)["status"], "failed")

    def test_opens_are_shown(self):
        eid = self.email("approved")
        self.post_action(eid, "send", confirm=self.row(eid)["recipient"])
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("select record_email_open(tracking_token, 'Apple Mail') from emails where id=%s", [eid])
        page = self.client.get(self.change_url(eid)).content.decode()
        self.assertIn("Apple Mail", page)
        self.assertIn("Opened", page)
        self.assertIn("100%", self.client.get(reverse("admin:pipeline_email_changelist")).content.decode())

    def test_posts_without_the_csrf_token_are_refused(self):
        from django.test import Client

        eid = self.email()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("admin:pipeline_email_approve", args=[eid]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.row(eid)["status"], "in_review")

    def test_foreign_host_header_is_refused(self):
        response = self.client.get(reverse("admin:pipeline_email_changelist"), HTTP_HOST="evil.example.com")
        self.assertEqual(response.status_code, 400)


class DashboardTests(AdminCase):
    def test_dashboard_counts_next_steps_and_runs(self):
        make_email()
        make_email("approved")
        PipelineRun.objects.create(command="verify_emails", arguments=["--limit", "5"], status="succeeded")
        page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("Emails in review", page)
        self.assertIn("1 email(s) are waiting for your review.", page)
        self.assertIn("Start reviewing", page)
        self.assertIn("1 approved email(s) are ready to send.", page)
        self.assertIn("verify_emails --limit 5", page)
        self.assertNotIn("Sending is not set up", page)  # SMTP is configured in these tests

    def test_empty_database_suggests_a_campaign_and_warns_about_settings(self):
        with mock.patch.dict(os.environ, {"SMTP_PASSWORD": "", "VERIFY_HELO": ""}):
            page = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("Create your first campaign", page)
        self.assertIn("Sending is not set up", page)
        self.assertIn("VERIFY_HELO is not set", page)

    def test_sidebar_follows_the_pipeline(self):
        apps = self.client.get(reverse("admin:index")).context["app_list"]
        self.assertEqual(apps[0]["app_label"], "pipeline")
        names = [m["object_name"] for m in apps[0]["models"]]
        self.assertEqual(names[:8], ["DiscoveryCampaign", "CampaignPrompt", "EmailPrompt", "Business",
                                     "BusinessWebsiteProfile", "BusinessContact", "Prospect", "Email"])

    def test_guide(self):
        page = self.client.get(reverse("admin:guide")).content.decode()
        self.assertIn("Step 7: Review emails", page)
        self.assertIn('class="toc"', page)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("admin:guide")).status_code, 302)

    def test_hand_added_contacts_are_marked_manual(self):
        business = make_business(name="B")
        self.client.post(reverse("admin:pipeline_businesscontact_add"), {
            "business": business.pk, "name": "Owner", "email": "o@b.org", "email_status": "unverified",
            "candidate_emails": "[]", "source_urls": "[]"})
        self.assertEqual(BusinessContact.objects.get().email_source, "manual")

    def test_run_form_has_a_start_button(self):
        page = self.client.get(reverse("admin:pipeline_pipelinerun_add")).content.decode()
        self.assertIn('value="Start run"', page)
        self.assertNotIn("Save and add another", page)


class ExternalLinkTests(AdminCase):
    def test_scraped_javascript_urls_never_become_links(self):
        evil = "javascript:alert(document.cookie)"
        email = make_email()
        Business.objects.filter(pk=email.business_id).update(website_url=evil, domain=None)
        BusinessWebsiteProfile.objects.filter(business=email.business).update(scraped_urls=[evil, "https://ok.pk/"])
        BusinessContact.objects.filter(pk=email.contact_id).update(source_urls=["JavaScript:alert(1)", "https://ok.pk/team"])
        pages = [
            reverse("admin:pipeline_business_changelist"),
            reverse("admin:pipeline_businesswebsiteprofile_change", args=[email.business.website_profile.pk]),
            reverse("admin:pipeline_businesscontact_change", args=[email.contact_id]),
            reverse("admin:pipeline_email_change", args=[email.pk]),
        ]
        for url in pages:
            page = self.client.get(url).content.decode()
            self.assertNotRegex(page.lower(), r'href="\s*javascript:', url)
        profile = self.client.get(pages[1]).content.decode()
        self.assertIn('href="https://ok.pk/"', profile)


class RunPageTests(AdminCase):
    def test_finished_run_is_read_only(self):
        run = PipelineRun.objects.create(command="research_websites", arguments=["--business-id", "1"],
                                         status="succeeded", output="done")
        url = reverse("admin:pipeline_pipelinerun_change", args=[run.pk])
        page = self.client.get(url).content.decode()
        self.assertNotIn('name="_save"', page)
        self.assertNotIn("Please correct the error", page)
        self.assertIn("Run again", page)
        self.assertIn("Delete", page)
        response = self.client.post(url, {"command": "send_emails"})
        self.assertEqual(response.status_code, 403)
        run.refresh_from_db()
        self.assertEqual(run.command, "research_websites")
