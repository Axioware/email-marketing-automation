"""The admin site: a pipeline dashboard as the home page, the sidebar in pipeline order, the lead Pipeline board and
the user guide."""
import os
from pathlib import Path

import markdown

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.safestring import mark_safe

GUIDE_PATH = Path(settings.BASE_DIR) / "docs" / "USER_GUIDE.md"
BOARD_CARDS_PER_STAGE = 200

# Sidebar order: the pipeline's own order, then the supporting tables. "pipelineboard" is the Pipeline page.
MODEL_ORDER = ["discoverycampaign", "campaignprompt", "emailprompt", "emailsequence", "business", "businesswebsiteprofile",
               "businesscontact", "prospect", "email", "outboxemail", "pipelineboard", "pipelinerun", "sendingsettings",
               "businesssource"]


def changelist(model_name: str, **filters) -> str:
    from urllib.parse import urlencode

    url = reverse(f"admin:pipeline_{model_name}_changelist")
    return f"{url}?{urlencode(filters)}" if filters else url


def run_url(command: str, arguments: str = "") -> str:
    from urllib.parse import urlencode

    return f"{reverse('admin:pipeline_pipelinerun_add')}?{urlencode({'command': command, 'arguments': arguments})}"


def lead_sources() -> dict:
    """The "Get leads" panel: recent campaigns with a fetch button per lead source, and whether the scraper container
    answers."""
    from pipeline.models import DiscoveryCampaign
    from pipeline.services import scraper_api

    def fetch(pk, source):
        return run_url("fetch_businesses", f"--campaign-id {pk} --limit 20 --source {source}")

    campaigns = DiscoveryCampaign.objects.annotate(business_total=Count("businesses")).order_by("-created_at")[:8]
    problem = scraper_api.status(timeout=1.5)
    return {
        "browser": True,
        "campaigns": [{"campaign": campaign, "browser_url": fetch(campaign.pk, "browser"),
                       "scraper_url": fetch(campaign.pk, "scraper_api"),
                       "url": reverse("admin:pipeline_discoverycampaign_change", args=[campaign.pk])}
                      for campaign in campaigns],
        "scraper_url": scraper_api.base_url(),
        "scraper_problem": problem,
        "scraper_needed": any(campaign.lead_source == "scraper_api" for campaign in campaigns),
    }


def sequence_results() -> list[dict]:
    """Sent emails per step and email sequence (the A/B group), with reply rates."""
    from pipeline.models import Email
    rows = (Email.objects.filter(sent_at__isnull=False)
            .values("sequence_step", "prospect__email_sequence__name")
            .annotate(sent=Count("id"), replied=Count("id", filter=Q(status="replied")))
            .order_by("prospect__email_sequence__name", "sequence_step"))
    return [{
        "step": "First email" if row["sequence_step"] == 1 else f"Follow-up {row['sequence_step'] - 1}",
        "variation": row["prospect__email_sequence__name"] or "-",
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
                app["models"].append({"name": "Pipeline", "object_name": "PipelineBoard", "perms": {"view": True},
                                      "admin_url": reverse("admin:pipeline_board"), "add_url": None,
                                      "view_only": True})
                app["models"].sort(key=lambda m: MODEL_ORDER.index(m["object_name"].lower())
                                   if m["object_name"].lower() in MODEL_ORDER else len(MODEL_ORDER))
        apps.sort(key=lambda app: app["app_label"] != "pipeline")  # the pipeline first
        return apps

    def get_urls(self):
        return [path("guide/", self.admin_view(self.guide_view), name="guide"),
                path("email-variables/", self.admin_view(self.variables_view), name="email_variables"),
                path("pipeline-board/", self.admin_view(self.board_view), name="pipeline_board"),
                path("pipeline-board/<int:prospect_id>/move/", self.admin_view(self.move_view),
                     name="pipeline_board_move")] + super().get_urls()

    # ------------------------------------------------------------ the lead pipeline

    def board_view(self, request):
        """Every lead in its stage, like GoHighLevel's opportunity pipeline. Opening it moves finished sequences with
        no reply to Quota Exhausted (stages.expire_finished)."""
        from pipeline.models import PipelineStage
        from pipeline.services import stages

        expired = stages.expire_finished()
        if expired:
            messages.info(request, f"{expired} lead(s) moved to Quota Exhausted (no reply within the reply window).")
        leads = stages.leads()
        campaign = request.GET.get("campaign") or ""
        if campaign.isdigit():
            leads = leads.filter(business__discovery_campaign_id=int(campaign))
        query = (request.GET.get("q") or "").strip()
        if query:
            leads = leads.filter(Q(email__icontains=query) | Q(business__name__icontains=query)
                                 | Q(contact__name__icontains=query))
        by_stage = {value: [] for value in PipelineStage.values}
        totals = dict.fromkeys(PipelineStage.values, 0)
        for lead in leads.order_by("-stage_changed_at", "-id"):
            stage = lead.stage
            totals[stage] += 1
            if len(by_stage[stage]) < BOARD_CARDS_PER_STAGE:
                by_stage[stage].append(lead)
        from pipeline.models import DiscoveryCampaign

        columns = [{"value": value, "label": label, "leads": by_stage[value], "total": totals[value],
                    "more": totals[value] - len(by_stage[value])} for value, label in PipelineStage.choices]
        context = {**self.each_context(request), "title": "Pipeline", "columns": columns, "stages": PipelineStage.choices,
                   "campaigns": DiscoveryCampaign.objects.order_by("name"),
                   "campaign": campaign, "query": query}
        return TemplateResponse(request, "admin/pipeline/board.html", context)

    def move_view(self, request, prospect_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        from pipeline.models import Prospect
        from pipeline.services import stages

        prospect = get_object_or_404(Prospect, pk=prospect_id)
        try:
            message = stages.move(prospect, request.POST.get("stage", ""), request.user, request.POST.get("note", ""))
            messages.success(request, f"{prospect.email}: {message}")
        except stages.StageError as error:
            messages.error(request, f"{prospect.email}: {error}")
        back = request.POST.get("next") or reverse("admin:pipeline_board")
        return HttpResponseRedirect(back if back.startswith("/") and not back.startswith("//") else reverse("admin:pipeline_board"))

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
            PipelineStage,
            Prospect,
        )
        from pipeline.services import generation, outreach, qualify, sending, stages
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
        stages.expire_finished()

        funnel = [
            ("Businesses found", businesses["total"], changelist("business"), "Module 1"),
            ("Websites researched", profiles["researched"], changelist("businesswebsiteprofile", status__exact="completed"), "Module 2"),
            ("Qualified", profiles["qualified"], changelist("business", qualification="qualified"), "Module 2"),
            ("Contacts found", BusinessContact.objects.count(), changelist("businesscontact"), "Module 3"),
            ("Ready to email", ready.count(), changelist("prospect", outreach_status__exact="ready"), "Module 4"),
            ("Emails in review", emails["in_review"], changelist("email", status__exact="in_review"), "Module 5"),
            ("Approved", emails["approved"], changelist("outboxemail", status="approved"), "Queue them in the Outbox"),
            ("Queued", emails["queued"] + emails["sending"], changelist("outboxemail", status="queued"),
             "Sent from your mailbox at their times"),
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
        if emails["approved"]:
            steps.append((f"{emails['approved']} approved email(s) are not queued: queue them to send them.",
                          "Open the Outbox", changelist("outboxemail", status="approved")))
        if emails["failed"]:
            steps.append((f"{emails['failed']} email(s) failed to send: check the reason, then queue them again.",
                          "Open the Outbox", changelist("outboxemail", status="failed")))
        if (emails["queued"] and sending.configured() and jobs.active_run("send_emails") is None):
            steps.append((f"{emails['queued']} email(s) are queued but the sender is not running.", "Open the Outbox",
                          changelist("outboxemail", status="queued")))
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
        if missing:
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
        from pipeline.models import SendingSettings
        from pipeline.services import hunter

        uses_hunter = SendingSettings.current().contact_provider == SendingSettings.ContactProvider.HUNTER
        if uses_hunter and not hunter.configured():
            warnings.append("Settings say to find and verify contacts with Hunter, but HUNTER_API_KEY is not set in "
                            ".env.")
        elif not uses_hunter and not os.environ.get("VERIFY_HELO"):
            warnings.append("VERIFY_HELO is not set in .env, so email verification cannot run.")
        if not uses_hunter and not os.environ.get("SEARXNG_URL"):
            warnings.append("SEARXNG_URL is not set: finding decision makers only reads the businesses' websites, "
                            "with no web search.")
        if not sending.configured():
            warnings.append(f"Sending is not set up: set {sending.missing_settings()} and the other SMTP_ settings in "
                            ".env; queued emails wait until then.")
        stuck = outreach.stuck_emails().count()
        if stuck:
            warnings.append(f"{stuck} email(s) were left half-sent by a sender that stopped: check your Sent folder "
                            "(they become Failed when the sender next starts).")
        leads = lead_sources()
        if leads["scraper_needed"] and leads["scraper_problem"]:
            warnings.append(f"A campaign gets its leads from the scraper container, but it is {leads['scraper_problem']} "
                            f"at {leads['scraper_url']}: start it, or set MAPS_SCRAPER_URL in .env.")

        return {
            "leads": leads,
            "pipeline": [{"label": label, "count": count, "url": f"{reverse('admin:pipeline_board')}#stage-{value}"}
                         for (value, label), count in zip(PipelineStage.choices, stages.counts().values())],
            "results": sequence_results(),
            "funnel": funnel,
            "steps": steps,
            "warnings": warnings,
            "recent_runs": PipelineRun.objects.select_related("created_by")[:6],
            "running": PipelineRun.objects.filter(status__in=["queued", "running"]).count(),
        }
