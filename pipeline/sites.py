"""The admin site: a pipeline dashboard as the home page, the sidebar in pipeline order, and the user guide."""
import os
from pathlib import Path

import markdown
from datetime import timedelta

from django.conf import settings
from django.contrib import admin
from django.db.models import Count, Q
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.safestring import mark_safe

GUIDE_PATH = Path(settings.BASE_DIR) / "docs" / "USER_GUIDE.md"
WORKER_STALE_AFTER = timedelta(minutes=2)  # no heartbeat for this long: the worker is shown as not running

# Sidebar order: the pipeline's own order, then the supporting tables.
MODEL_ORDER = ["discoverycampaign", "campaignprompt", "emailprompt", "business", "businesswebsiteprofile", "businesscontact", "prospect", "email",
               "pipelinerun", "sendingsettings", "businesssource"]


def changelist(model_name: str, **filters) -> str:
    from urllib.parse import urlencode

    url = reverse(f"admin:pipeline_{model_name}_changelist")
    return f"{url}?{urlencode(filters)}" if filters else url


def run_url(command: str, arguments: str = "") -> str:
    from urllib.parse import urlencode

    return f"{reverse('admin:pipeline_pipelinerun_add')}?{urlencode({'command': command, 'arguments': arguments})}"


def lead_sources() -> dict:
    """The "Get leads" panel: recent campaigns with a fetch button per lead source (the built-in browser only where
    Chromium is available, not on Vercel), and whether the scraper container answers."""
    from pipeline.models import DiscoveryCampaign
    from pipeline.services import scraper_api

    def fetch(pk, source):
        return run_url("fetch_businesses", f"--campaign-id {pk} --limit 20 --source {source}")

    campaigns = DiscoveryCampaign.objects.annotate(business_total=Count("businesses")).order_by("-created_at")[:8]
    problem = scraper_api.status(timeout=1.5)
    return {
        "browser": not settings.ON_VERCEL,
        "campaigns": [{"campaign": campaign, "browser_url": fetch(campaign.pk, "browser"),
                       "scraper_url": fetch(campaign.pk, "scraper_api"),
                       "url": reverse("admin:pipeline_discoverycampaign_change", args=[campaign.pk])}
                      for campaign in campaigns],
        "scraper_url": scraper_api.base_url(),
        "scraper_problem": problem,
        "scraper_needed": any(campaign.lead_source == "scraper_api" for campaign in campaigns),
    }


def worker_status() -> dict:
    """The most recent worker heartbeat (queue mode: runs execute on a worker machine)."""
    from django.utils import timezone

    from pipeline.models import WorkerHeartbeat

    beat = WorkerHeartbeat.objects.order_by("-last_seen").first()
    if beat is None:
        return {"alive": False, "name": None, "last_seen": None, "info": {}}
    return {"alive": timezone.now() - beat.last_seen < WORKER_STALE_AFTER, "name": beat.name,
            "last_seen": beat.last_seen, "info": beat.info or {}}


def sequence_results() -> list[dict]:
    """Sent emails per step and initial-email template (the A/B group), with reply rates."""
    from pipeline.models import Email
    rows = (Email.objects.filter(sent_at__isnull=False)
            .values("sequence_step", "prospect__initial_email_prompt__name")
            .annotate(sent=Count("id"), replied=Count("id", filter=Q(status="replied")))
            .order_by("sequence_step", "prospect__initial_email_prompt__name"))
    return [{
        "step": "First email" if row["sequence_step"] == 1 else f"Follow-up {row['sequence_step'] - 1}",
        "variation": row["prospect__initial_email_prompt__name"] or "-",
        "sent": row["sent"],
        "replied": row["replied"],
        "reply_rate": round(100 * row["replied"] / row["sent"]),
    } for row in rows]


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
        return [path("guide/", self.admin_view(self.guide_view), name="guide"),
                path("email-variables/", self.admin_view(self.variables_view), name="email_variables")] + super().get_urls()

    def variables_view(self, request):
        """Every variable an email template can use, with its value for one real prospect."""
        from pipeline.models import Prospect
        from pipeline.services import generation, templates

        prospect = generation.ready_prospects().order_by("-id").first() or Prospect.objects.order_by("-id").first()
        example, values = None, {}
        if prospect is not None:
            row = generation.prospect_rows(Prospect.objects.filter(pk=prospect.pk))[0]
            row["previous_subject"] = "Quick question for your clinic"
            values = templates.values_for(row)
            example = f"{row['business_name']} ({row['recipient']})"
        research = templates.research_fields()
        rows = [{"name": name, "token": "{{" + name + "}}", "description": description, "example": values.get(name),
                 "group": "Research (campaign prompt output fields)" if name in research and name not in templates.VARIABLES
                 else "Prospect, business and campaign"}
                for name, description in templates.known_variables().items()]
        context = {**self.each_context(request), "title": "Email variables", "rows": rows, "example": example,
                   "plain": "{{first_name}}", "with_fallback": "{{first_name|there}}"}
        return TemplateResponse(request, "admin/pipeline/email_variables.html", context)

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
            Email,
            PipelineRun,
            Prospect,
        )
        from pipeline.services import generation, ghl, outreach, qualify
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
            discovered=Count("id", filter=Q(contact_discovery_status="completed")),
        )
        qualified = Business.objects.filter(qualify.QUALIFIED)
        profiles["qualified"] = qualified.count()
        profiles["to_discover"] = qualified.filter(Q(website_profile__isnull=True)
                                                   | Q(website_profile__contact_discovery_status__isnull=True)).count()
        business_reviews = Business.objects.filter(qualify.NEEDS_REVIEW).count()
        to_research = Business.objects.filter(website_url__isnull=False).exclude(website_url="").filter(
            Q(website_profile__isnull=True) | Q(website_profile__status="pending")).count()
        unchecked_contacts = BusinessContact.objects.filter(
            Q(email_status__isnull=True) | Q(email_status__in=["unverified", "unknown"])).count()
        ready = generation.ready_prospects()
        ready_without_email = ready.filter(emails__isnull=True).count()
        prospect_reviews = Prospect.objects.filter(outreach_status="needs_review", do_not_contact=False).count()
        scheduled = emails["scheduled"] + emails["sending"]

        funnel = [
            ("Businesses found", businesses["total"], changelist("business"), "Module 1"),
            ("Websites researched", profiles["researched"], changelist("businesswebsiteprofile", status__exact="completed"), "Module 2"),
            ("Qualified", profiles["qualified"], changelist("business", qualification="qualified"), "Module 2"),
            ("Contacts found", BusinessContact.objects.count(), changelist("businesscontact"), "Module 3"),
            ("Ready to email", ready.count(), changelist("prospect", outreach_status__exact="ready"), "Module 4"),
            ("Emails in review", emails["in_review"], changelist("email", status__exact="in_review"), "Module 5"),
            ("Scheduled", scheduled, changelist("email", status__exact="scheduled"), "GoHighLevel sends them"),
            ("Sent", sent, changelist("email", status__exact="sent"),
             f"{emails['replied']} replied" if emails["replied"] else "Delivery"),
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
        from pipeline.services import followups

        due_followups = len(followups.find_due({}))
        if due_followups:
            steps.append((f"{due_followups} follow-up(s) can be written for review.", "Write follow-ups",
                          run_url("generate_followups")))
        if business_reviews:
            steps.append((f"{business_reviews} business(es) scored below their campaign's minimum or could not be "
                          "researched: approve or reject them.", "Review businesses",
                          changelist("business", qualification="review")))
        not_handed = outreach.not_handed_over().count()
        if not_handed and ghl.configured():
            steps.append((f"{not_handed} approved email(s) are not in GoHighLevel yet (the hand-over failed or was "
                          "interrupted): hand them over.", "Hand to GoHighLevel", run_url("ghl_sync")))
        missing_contacts = ready.filter(ghl_contact_id__isnull=True).count()
        if missing_contacts and ghl.configured():
            steps.append((f"{missing_contacts} ready prospect(s) are not GoHighLevel contacts yet.",
                          "Create GHL contacts", run_url("ghl_sync", "--contacts-only")))
        if prospect_reviews:
            steps.append((f"{prospect_reviews} prospect address(es) could not be verified: approve or reject them.",
                          "Review prospects", changelist("prospect", outreach_status__exact="needs_review")))
        if ready_without_email:
            steps.append((f"{ready_without_email} verified prospect(s) have no email yet.", "Write emails",
                          reverse("admin:pipeline_prospect_generate")))
        if unchecked_contacts:
            steps.append((f"{unchecked_contacts} contact(s) have emails that are not verified yet.", "Verify emails",
                          run_url("verify_emails")))
        if profiles["to_discover"]:
            steps.append((f"{profiles['to_discover']} qualified business(es) still need a decision maker.",
                          "Find decision makers", run_url("find_stakeholders")))
        missing = Business.objects.filter(google_review_count__isnull=True).count()
        if missing and not settings.ON_VERCEL:  # refreshing Maps details needs the built-in browser
            steps.append((f"{missing} business(es) have no Google review count or category yet.",
                          "Refresh Google Maps details", run_url("refresh_businesses")))
        if to_research:
            steps.append((f"{to_research} business(es) with a website have not been researched.", "Research websites",
                          run_url("research_websites")))
        if profiles["failed"]:
            steps.append((f"Website research failed for {profiles['failed']} business(es).", "Retry failed",
                          run_url("research_websites", "--retry-failed")))

        warnings = []
        if not settings.AUTH_TOKEN:
            warnings.append("The API is disabled: set AUTH_TOKEN in .env.")
        if make_llm_client() is None:
            warnings.append("No AI key: set GROQ_API_KEY or OPENAI_API_KEY (needed to research websites; emails are "
                            "written from templates).")
        if settings.ON_VERCEL and not (os.environ.get("REACHER_URL") and os.environ.get("REACHER_SECRET")):
            warnings.append("Email verification is off: set REACHER_URL and REACHER_SECRET (your server's Reacher, "
                            "once outbound port 25 is open there).")
        elif not settings.ON_VERCEL and not os.environ.get("VERIFY_HELO"):
            warnings.append("VERIFY_HELO is not set in .env, so email verification cannot run.")
        if not os.environ.get("SEARXNG_URL"):
            warnings.append("SEARXNG_URL is not set: finding decision makers only reads the businesses' websites, "
                            "with no web search.")
        if not ghl.configured():
            warnings.append("GoHighLevel is not set up: add GHL_API_KEY and GHL_LOCATION_ID to .env, or approved emails "
                            "are never sent.")
        if not os.environ.get("GHL_WEBHOOK_SECRET"):
            warnings.append("GHL_WEBHOOK_SECRET is not set: GoHighLevel cannot report sent emails or replies.")
        stuck = outreach.stuck_emails().count()
        if stuck:
            warnings.append(f"{stuck} email(s) in GoHighLevel are over a day past their send time and were never reported "
                            "sent: check the sending workflow (published?) and its email-sent webhook.")
        worker = worker_status()
        uses_worker = jobs.queue_mode() and not settings.ON_VERCEL  # on Vercel every run executes in its request
        if uses_worker and not worker["alive"]:
            warnings.append("No worker is running: queued runs wait until `python manage.py run_worker` is started on "
                            "the machine with the browsers and Docker.")
        leads = lead_sources()
        if leads["scraper_needed"] and leads["scraper_problem"]:
            warnings.append(f"A campaign gets its leads from the scraper container, but it is {leads['scraper_problem']} "
                            f"at {leads['scraper_url']}: start it, or set MAPS_SCRAPER_URL in .env.")

        return {
            "leads": leads,
            "results": sequence_results(),
            "funnel": funnel,
            "steps": steps,
            "warnings": warnings,
            "recent_runs": PipelineRun.objects.select_related("created_by")[:6],
            "running": PipelineRun.objects.filter(status__in=["queued", "running"]).count(),
        }
