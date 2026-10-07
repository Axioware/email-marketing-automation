"""The admin site: a pipeline dashboard as the home page, the sidebar in pipeline order, and the user guide."""
import os
from pathlib import Path

import markdown
from django.conf import settings
from django.contrib import admin
from django.db.models import Count, Q
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.safestring import mark_safe

GUIDE_PATH = Path(settings.BASE_DIR) / "docs" / "USER_GUIDE.md"
QUALIFIED_SCORE = 50  # find_stakeholders' default --min-score

# Sidebar order: the pipeline's own order, then the supporting tables.
MODEL_ORDER = ["discoverycampaign", "business", "businesswebsiteprofile", "businesscontact", "prospect", "email",
               "pipelinerun", "emailopenevent", "businesssource"]


def changelist(model_name: str, **filters) -> str:
    from urllib.parse import urlencode

    url = reverse(f"admin:pipeline_{model_name}_changelist")
    return f"{url}?{urlencode(filters)}" if filters else url


def run_url(command: str, arguments: str = "") -> str:
    from urllib.parse import urlencode

    return f"{reverse('admin:pipeline_pipelinerun_add')}?{urlencode({'command': command, 'arguments': arguments})}"


class PipelineAdminSite(admin.AdminSite):
    site_header = "Email Marketing Automation"
    site_title = "Email Marketing Automation"
    index_title = "Dashboard"
    index_template = "admin/pipeline/dashboard.html"

    def get_app_list(self, request, app_label=None):
        apps = super().get_app_list(request, app_label)
        for app in apps:
            if app["app_label"] == "pipeline":
                app["models"].sort(key=lambda m: MODEL_ORDER.index(m["object_name"].lower())
                                   if m["object_name"].lower() in MODEL_ORDER else len(MODEL_ORDER))
        apps.sort(key=lambda app: app["app_label"] != "pipeline")  # the pipeline first
        return apps

    def get_urls(self):
        return [path("guide/", self.admin_view(self.guide_view), name="guide")] + super().get_urls()

    def guide_view(self, request):
        try:
            text = GUIDE_PATH.read_text(encoding="utf-8")
        except OSError:
            text = "# User guide\n\n`docs/USER_GUIDE.md` is missing."
        converter = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"])
        body = converter.convert(text)
        html = body.replace("</h1>", "</h1>" + converter.toc, 1)  # contents box after the title
        context = {**self.each_context(request), "title": "User guide", "guide": mark_safe(html)}  # noqa: S308 - our own file
        return TemplateResponse(request, "admin/pipeline/guide.html", context)

    def index(self, request, extra_context=None):
        return super().index(request, {**(extra_context or {}), **self.dashboard()})

    # ------------------------------------------------------------ dashboard data

    def dashboard(self) -> dict:
        from pipeline import jobs, review
        from pipeline.models import (
            Business,
            BusinessContact,
            BusinessWebsiteProfile,
            DiscoveryCampaign,
            PipelineRun,
            Prospect,
        )
        from pipeline.services import generation, sending
        from pipeline.services.llm import make_llm_client

        jobs.refresh_stale_runs()
        emails = review.status_counts()
        sent = emails["sent"] + emails["opened"]
        businesses = Business.objects.aggregate(
            total=Count("id"),
            with_site=Count("id", filter=Q(website_url__isnull=False) & ~Q(website_url="")),
        )
        profiles = BusinessWebsiteProfile.objects.aggregate(
            researched=Count("id", filter=Q(status="completed")),
            failed=Count("id", filter=Q(status="failed")),
            qualified=Count("id", filter=Q(status="completed", qualification_score__gte=QUALIFIED_SCORE)),
            discovered=Count("id", filter=Q(contact_discovery_status="completed")),
            to_discover=Count("id", filter=Q(status="completed", qualification_score__gte=QUALIFIED_SCORE,
                                             contact_discovery_status__isnull=True)),
        )
        to_research = Business.objects.filter(website_url__isnull=False).exclude(website_url="").filter(
            Q(website_profile__isnull=True) | Q(website_profile__status="pending")).count()
        unchecked_contacts = BusinessContact.objects.filter(
            Q(email_status__isnull=True) | Q(email_status__in=["unverified", "unknown"])).count()
        ready = Prospect.objects.filter(email_status="deliverable", outreach_status="ready", do_not_contact=False)
        ready_without_email = ready.filter(emails__isnull=True).count()

        funnel = [
            ("Businesses found", businesses["total"], changelist("business"), "Module 1"),
            ("Websites researched", profiles["researched"], changelist("businesswebsiteprofile", status__exact="completed"), "Module 2"),
            (f"Qualified (score {QUALIFIED_SCORE}+)", profiles["qualified"], changelist("business"), "Module 2"),
            ("Contacts found", BusinessContact.objects.count(), changelist("businesscontact"), "Module 3"),
            ("Verified prospects", ready.count(), changelist("prospect", outreach_status__exact="ready"), "Module 4"),
            ("Emails in review", emails["in_review"], changelist("email", status__exact="in_review"), "Module 5"),
            ("Sent", sent, changelist("email", status__exact="sent"), "Delivery"),
            ("Opened", emails["opened"], changelist("email", status__exact="opened"),
             f"{review.open_rate(emails)}% open rate" if sent else "Open rate -"),
        ]

        steps = []
        if not DiscoveryCampaign.objects.exists():
            steps.append(("Create your first campaign: what to search for and where.", "Create campaign",
                          reverse("admin:pipeline_discoverycampaign_add")))
        elif not businesses["total"]:
            steps.append(("Fetch businesses from Google Maps for your campaign.", "Open campaigns", changelist("discoverycampaign")))
        if emails["in_review"]:
            first = review.next_in_review(0)
            steps.append((f"{emails['in_review']} email(s) are waiting for your review.", "Start reviewing",
                          reverse("admin:pipeline_email_change", args=[first]) if first else changelist("email")))
        if emails["approved"]:
            steps.append((f"{emails['approved']} approved email(s) are ready to send.", "Send approved",
                          run_url("send_emails", "--send")))
        if ready_without_email:
            steps.append((f"{ready_without_email} verified prospect(s) have no email yet.", "Generate emails",
                          run_url("generate_emails")))
        if unchecked_contacts:
            steps.append((f"{unchecked_contacts} contact(s) have emails that are not verified yet.", "Verify emails",
                          run_url("verify_emails")))
        if profiles["to_discover"]:
            steps.append((f"{profiles['to_discover']} qualified business(es) still need a decision maker.",
                          "Find decision makers", run_url("find_stakeholders")))
        if to_research:
            steps.append((f"{to_research} business(es) with a website have not been researched.", "Research websites",
                          run_url("research_websites")))
        if profiles["failed"]:
            steps.append((f"Website research failed for {profiles['failed']} business(es).", "Retry failed",
                          run_url("research_websites", "--retry-failed")))

        warnings = []
        if make_llm_client() is None:
            warnings.append("No LLM key: set GROQ_API_KEY or OPENAI_API_KEY in .env (needed to research and write emails).")
        if not os.environ.get("VERIFY_HELO"):
            warnings.append("VERIFY_HELO is not set in .env, so email verification cannot run.")
        if not generation.default_tracking_base_url():
            warnings.append("The open-tracking URL is unknown: set EMAIL_TRACKING_BASE_URL in .env.")
        problems = sending.config_problems(sending.smtp_config())
        if problems:
            warnings.append("Sending is not set up: " + "; ".join(problems) + ".")

        return {
            "funnel": funnel,
            "steps": steps,
            "warnings": warnings,
            "recent_runs": PipelineRun.objects.select_related("created_by")[:6],
            "running": PipelineRun.objects.filter(status__in=["queued", "running"]).count(),
        }
