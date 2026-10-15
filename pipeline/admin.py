"""Django admin for the whole pipeline.

Every pipeline step can be started from here: list actions and buttons open the "Run a pipeline step" form, pre-filled
with the command and arguments (e.g. the selected businesses), where the run is confirmed and then followed live.
Generated emails are reviewed on their own page: edit, preview, approve, reject and regenerate.
"""
import json
import shlex
from urllib.parse import urlencode, urlsplit

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator

from pipeline import jobs, review
from pipeline.models import (
    Business,
    CampaignPrompt,
    EmailPrompt,
    FollowUp,
    BusinessContact,
    BusinessSource,
    BusinessWebsiteProfile,
    DiscoveryCampaign,
    Email,
    EmailOpenEvent,
    PipelineRun,
    Prospect,
)
from pipeline.services import generation, qualify


# ---------------------------------------------------------------- helpers


def admin_url(obj) -> str:
    return reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])


def admin_link(obj, label=None):
    if obj is None:
        return "-"
    return format_html('<a href="{}">{}</a>', admin_url(obj), label if label is not None else str(obj))


def changelist_link(model, label, **filters):
    url = reverse(f"admin:pipeline_{model._meta.model_name}_changelist") + "?" + urlencode(filters)
    return format_html('<a href="{}">{}</a>', url, label)


def safe_url(url) -> str | None:
    """The URL if it is http(s), else None: scraped data must never become a javascript: or data: link."""
    try:
        return url if isinstance(url, str) and urlsplit(url.strip()).scheme.lower() in ("http", "https") else None
    except ValueError:
        return None


def external_link(url, label=None):
    """A new-tab link for http(s) URLs; anything else is shown as plain text."""
    if not url:
        return "-"
    if safe_url(url) is None:
        return format_html("{}", label or url)
    return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>', url, label or url)


def pretty_json(value):
    if value in (None, [], {}):
        return "-"
    return format_html('<pre class="pipeline-pre">{}</pre>', json.dumps(value, indent=2, ensure_ascii=False, default=str))


def badge(value: str, label: str | None = None):
    return format_html('<span class="pipeline-badge pipeline-{}">{}</span>', value or "none", label or value or "-")


def run_form_url(command: str, arguments: list) -> str:
    query = urlencode({"command": command, "arguments": shlex.join(str(a) for a in arguments)})
    return f"{reverse('admin:pipeline_pipelinerun_add')}?{query}"


def fetch_url(campaign_id: int, source: str | None = None, limit: int = 20) -> str:
    """The run form for fetching a campaign's businesses, with a lead source or the campaign's own."""
    return run_form_url("fetch_businesses", ["--campaign-id", str(campaign_id), "--limit", str(limit),
                                             *(["--source", source] if source else [])])


def ids_arguments(flag: str, ids) -> list[str]:
    return [item for pk in sorted(set(ids)) for item in (flag, str(pk))]


def generate_url(scope: str | None = None, ids=(), regenerate: bool = False) -> str:
    """The "Generate emails" page (choose an email prompt), optionally for selected businesses or prospects."""
    query = {}
    if scope:
        query.update(scope=scope, ids=",".join(str(pk) for pk in sorted(set(ids))))
    if regenerate:
        query["regenerate"] = "1"
    return reverse("admin:pipeline_prospect_generate") + (f"?{urlencode(query)}" if query else "")


def run_step(command: str, arguments: list):
    """Admin action helper: open the run form pre-filled, so the run is reviewed and confirmed before it starts."""
    return HttpResponseRedirect(run_form_url(command, arguments))


class PipelineAdmin(admin.ModelAdmin):
    list_per_page = 50


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------- Module 1: campaigns and businesses


CAMPAIGN_SETTINGS = ("lead_source", "min_score", "gap_fixed_seconds", "gap_random_min_seconds",
                     "gap_random_max_seconds", "daily_cap", "send_timezone")  # left out of a form post: the default


class DiscoveryCampaignForm(forms.ModelForm):
    target_locations = forms.CharField(required=False, help_text="Comma-separated, e.g. Lahore, Karachi.")
    search_terms = forms.CharField(required=False, help_text="Comma-separated, e.g. dental clinic, dentist.")

    class Meta:
        model = DiscoveryCampaign
        fields = ["name", "target_country", "target_locations", "search_terms", "lead_source", "campaign_prompt",
                  "initial_email_prompts", "min_score", "gap_fixed_seconds", "gap_random_min_seconds",
                  "gap_random_max_seconds", "daily_cap", "send_window_start", "send_window_end", "send_timezone"]
        widgets = {"initial_email_prompts": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "campaign_prompt" in self.fields:
            self.fields["campaign_prompt"].help_text = (
                "The research agent's instructions for this campaign's businesses (Campaign prompts). Without one, "
                "they are not researched.")
        for field in CAMPAIGN_SETTINGS:
            if field in self.fields:
                self.fields[field].required = False
        for field in ("target_locations", "search_terms"):
            value = getattr(self.instance, field, None)
            if isinstance(value, list):
                self.initial[field] = ", ".join(str(item) for item in value)

    def _split(self, field):
        return [item.strip() for item in (self.cleaned_data.get(field) or "").split(",") if item.strip()]

    def clean_target_locations(self):
        return self._split("target_locations")

    def clean_search_terms(self):
        return self._split("search_terms")

    def clean_target_country(self):
        country = (self.cleaned_data.get("target_country") or "").strip()
        if "," in country:
            raise forms.ValidationError("Enter one country only.")
        return country or None

    def clean(self):
        cleaned = super().clean()
        for field in CAMPAIGN_SETTINGS:
            if field in self.fields and cleaned.get(field) in (None, ""):
                cleaned[field] = DiscoveryCampaign._meta.get_field(field).default
        return cleaned


class FollowUpInline(admin.TabularInline):
    model = FollowUp
    extra = 0
    fields = ["sequence", "days_after_previous", "email_prompt"]
    verbose_name_plural = ("follow-ups (the initial email is step 0; follow-up 1 is written for review once the "
                           "initial email is sent and goes out no earlier than its days later)")


def prompt_problems(campaign) -> list[str]:
    """What a campaign is missing to qualify its businesses and write its emails and follow-ups."""
    if campaign is None or not campaign.pk:
        return []
    problems = []
    if campaign.campaign_prompt_id is None:
        problems.append("This campaign has no campaign prompt: its businesses will not be researched. Choose one "
                        "under Prompts.")
    if not campaign.initial_email_prompts.exists():
        problems.append("This campaign has no initial-email prompt: no emails will be written. Choose at least one "
                        "under Prompts (several are split equally as an A/B test).")
    problems += [f"Follow-up {step.sequence} has no email prompt; the sequence ends before it. Choose one in the "
                 "Follow-ups table." for step in campaign.follow_ups.filter(email_prompt__isnull=True)]
    return problems


@admin.register(DiscoveryCampaign)
class DiscoveryCampaignAdmin(PipelineAdmin):
    form = DiscoveryCampaignForm
    list_display = ["name", "target_country", "locations", "terms", "lead_source", "status_badge", "business_count",
                    "created_at"]
    list_filter = ["status", "lead_source", "target_country"]
    search_fields = ["name"]
    readonly_fields = ["status", "created_at", "updated_at", "started_at", "completed_at", "businesses_link"]
    fieldsets = [
        (None, {"fields": ["name", "target_country", "target_locations", "search_terms"]}),
        ("Leads", {"fields": ["lead_source", "min_score"],
                   "description": "Built-in browser: this app opens Google Maps in its own Chromium. Scraper "
                                  "container: the Google Maps scraper Docker service does it (MAPS_SCRAPER_URL, port "
                                  "8001 by default). Both save the same details. Businesses scoring below the minimum "
                                  "score wait for you to approve or reject them (Businesses > Needs review)."}),
        ("Prompts", {"fields": ["campaign_prompt", "initial_email_prompts"],
                     "description": "Campaign prompt: qualifies businesses. Initial-email prompts: write the first "
                                    "email; with several, prospects are split equally (A/B test) and each keeps its "
                                    "prompt. Follow-up prompts are chosen per step in the Follow-ups table below."}),
        ("Sending", {"fields": [("gap_fixed_seconds", "gap_random_min_seconds", "gap_random_max_seconds"), "daily_cap",
                                ("send_window_start", "send_window_end"), "send_timezone"],
                     "description": "Each approved email gets a send time: the campaign's previous one plus the fixed "
                                    "gap plus a random gap between the two values (e.g. 120 s + 180-300 s), only inside "
                                    "the send window, and at most the daily cap per day. GoHighLevel sends it then."}),
        ("Progress", {"fields": ["status", "businesses_link", "started_at", "completed_at", "created_at", "updated_at"]}),
    ]
    actions = ["run_pipeline", "fetch_businesses", "fetch_with_browser", "fetch_with_scraper"]
    inlines = [FollowUpInline]
    change_form_template = "admin/pipeline/discoverycampaign/change_form.html"

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if request.method == "GET":
            for problem in prompt_problems(DiscoveryCampaign.objects.filter(pk=object_id).first()):
                self.message_user(request, problem, messages.WARNING)
        return super().change_view(request, object_id, form_url, extra_context)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(business_total=Count("businesses"))

    @admin.display(description="Locations")
    def locations(self, obj):
        return ", ".join(map(str, obj.target_locations or [])) or "-"

    @admin.display(description="Search terms")
    def terms(self, obj):
        return ", ".join(map(str, obj.search_terms or [])) or "-"

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Businesses", ordering="business_total")
    def business_count(self, obj):
        return changelist_link(Business, obj.business_total, discovery_campaign__id__exact=obj.pk)

    @admin.display(description="Businesses")
    def businesses_link(self, obj):
        if not obj.pk:
            return "-"
        return changelist_link(Business, f"{obj.businesses.count()} businesses", discovery_campaign__id__exact=obj.pk)

    @admin.action(description="Run the full pipeline (fetch → research → decision makers → verify → write emails)")
    def run_pipeline(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one campaign.", messages.WARNING)
            return None
        return run_step("run_pipeline", ["--campaign-id", str(queryset.first().pk), "--fetch-limit", "20"])

    def fetch_one(self, request, queryset, source=None):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one campaign.", messages.WARNING)
            return None
        return HttpResponseRedirect(fetch_url(queryset.first().pk, source))

    @admin.action(description="Fetch businesses from Google Maps (the campaign's lead source)")
    def fetch_businesses(self, request, queryset):
        return self.fetch_one(request, queryset)

    @admin.action(description="Fetch businesses with the built-in browser")
    def fetch_with_browser(self, request, queryset):
        return self.fetch_one(request, queryset, "browser")

    @admin.action(description="Fetch businesses with the scraper container")
    def fetch_with_scraper(self, request, queryset):
        return self.fetch_one(request, queryset, "scraper_api")

    def render_change_form(self, request, context, *args, **kwargs):
        obj = context.get("original")
        if obj is not None:
            context["fetch_browser_url"] = fetch_url(obj.pk, "browser")
            context["fetch_scraper_url"] = fetch_url(obj.pk, "scraper_api")
            context["pipeline_url"] = run_form_url("run_pipeline", ["--campaign-id", str(obj.pk), "--fetch-limit", "20"])
        return super().render_change_form(request, context, *args, **kwargs)


class CampaignPromptForm(forms.ModelForm):
    class Meta:
        model = CampaignPrompt
        fields = ["name", "prompt"]
        widgets = {"prompt": forms.Textarea(attrs={"rows": 24, "class": "vLargeTextField"})}


@admin.register(CampaignPrompt)
class CampaignPromptAdmin(PipelineAdmin):
    form = CampaignPromptForm
    list_display = ["name", "campaigns_list", "updated_at"]
    search_fields = ["name", "prompt"]
    readonly_fields = ["campaigns_list", "created_at", "updated_at", "answer_format_display"]
    fieldsets = [
        (None, {"fields": ["name", "campaigns_list", "prompt"],
                "description": "The research agent's instructions, used exactly as written (nothing is added): it "
                               "reads each business's website and scores it 0-100 by this text; businesses scoring at "
                               "least their campaign's minimum score go on to finding decision makers. Only the shape "
                               "of its answer is set by the app (see Answer format)."}),
        ("Answer format (set by the app)", {"fields": ["answer_format_display"], "classes": ["collapse"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]

    @admin.display(description="Used by campaigns")
    def campaigns_list(self, obj):
        if not obj.pk:
            return "-"
        campaigns = list(obj.campaigns.all())
        return format_html_join(", ", "{}", ((admin_link(c),) for c in campaigns)) if campaigns else "Not used yet"

    @admin.display(description="JSON schema")
    def answer_format_display(self, obj):
        from pipeline.services.research import answer_format

        return format_html('<pre class="pipeline-pre">{}</pre>', answer_format())


class EmailPromptForm(forms.ModelForm):
    class Meta:
        model = EmailPrompt
        fields = ["name", "kind", "prompt"]
        widgets = {"prompt": forms.Textarea(attrs={"rows": 18, "class": "vLargeTextField"})}

    def clean_kind(self):
        kind = self.cleaned_data["kind"]
        instance = self.instance
        if instance.pk and kind != instance.kind:
            if kind == "follow_up" and instance.initial_for_campaigns.exists():
                raise forms.ValidationError("Campaigns use it for their initial email; remove it there first.")
            if kind == "initial" and instance.follow_up_steps.exists():
                raise forms.ValidationError("Campaigns use it for a follow-up step; remove it there first.")
        return kind


@admin.register(EmailPrompt)
class EmailPromptAdmin(PipelineAdmin):
    form = EmailPromptForm
    list_display = ["name", "kind", "used_by", "email_count", "updated_at"]
    list_filter = ["kind"]
    search_fields = ["name", "prompt"]
    readonly_fields = ["used_by", "created_at", "updated_at"]
    fieldsets = [
        (None, {"fields": ["name", "kind", "used_by", "prompt"],
                "description": "Writes one kind of email: the model receives exactly this text, with the prospect's "
                               "details (and, for follow-ups, follow_up and previous_emails) as input. Campaigns choose "
                               "initial-email prompts (several = A/B test) and one follow-up prompt per step."}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(email_total=Count("emails"))

    @admin.display(description="Used by")
    def used_by(self, obj):
        if not obj.pk:
            return "-"
        uses = [(admin_link(c), "initial email") for c in obj.initial_for_campaigns.all()]
        uses += [(admin_link(step.campaign), f"follow-up {step.sequence}")
                 for step in obj.follow_up_steps.select_related("campaign")]
        return format_html_join(", ", "{} ({})", uses) if uses else "Not used yet"

    @admin.display(description="Emails written", ordering="email_total")
    def email_count(self, obj):
        return changelist_link(Email, obj.email_total, email_prompt__id__exact=obj.pk)


class BusinessSourceInline(ReadOnlyInline):
    model = BusinessSource
    fields = ["source", "source_business_id", "source_url", "discovered_at"]
    readonly_fields = fields
    classes = ["collapse"]


class BusinessContactInline(ReadOnlyInline):
    model = BusinessContact
    fields = ["name", "job_title", "email", "email_source", "email_status", "is_primary", "confidence"]
    readonly_fields = fields


class ProspectInline(ReadOnlyInline):
    model = Prospect
    fk_name = "business"
    fields = ["email", "contact", "email_status", "verdict", "outreach_status", "do_not_contact", "last_contacted_at"]
    readonly_fields = fields


class EmailInline(ReadOnlyInline):
    model = Email
    fk_name = "business"
    fields = ["recipient", "subject", "status", "sent_at", "open_count"]
    readonly_fields = fields


class HasWebsiteFilter(admin.SimpleListFilter):
    title = "website"
    parameter_name = "has_website"

    def lookups(self, request, model_admin):
        return [("yes", "Has a website"), ("no", "No website")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(website_url__isnull=False).exclude(website_url="")
        if self.value() == "no":
            return queryset.filter(website_url__isnull=True) | queryset.filter(website_url="")
        return queryset


class ChoiceFilter(admin.SimpleListFilter):
    """A list filter defined by (value, label, Q) options."""

    options: list = []

    def lookups(self, request, model_admin):
        return [(value, label) for value, label, _ in self.options]

    def queryset(self, request, queryset):
        for value, _, condition in self.options:
            if self.value() == value:
                return queryset.filter(condition).distinct()
        return queryset


class ResearchStatusFilter(ChoiceFilter):
    title = "website research"
    parameter_name = "research"
    options = [
        ("none", "Not researched", Q(website_profile__isnull=True) | Q(website_profile__status="pending")),
        ("running", "Running", Q(website_profile__status="running")),
        ("completed", "Researched", Q(website_profile__status="completed")),
        ("failed", "Failed", Q(website_profile__status="failed")),
    ]


class ReviewFilter(ChoiceFilter):
    title = "qualification"
    parameter_name = "qualification"
    options = [
        ("review", "Needs your review", qualify.NEEDS_REVIEW),
        ("qualified", "Qualified", qualify.QUALIFIED),
        ("approved", "Approved by a person", Q(review_status="approved")),
        ("rejected", "Rejected by a person", Q(review_status="rejected")),
    ]


class ScoreFilter(ChoiceFilter):
    title = "qualification score"
    parameter_name = "score"
    options = [
        ("80", "80 and above", Q(website_profile__qualification_score__gte=80)),
        ("50up", "50 and above (qualified)", Q(website_profile__qualification_score__gte=50)),
        ("50", "50 to 79", Q(website_profile__qualification_score__gte=50, website_profile__qualification_score__lt=80)),
        ("low", "Below 50", Q(website_profile__qualification_score__lt=50)),
        ("none", "Not scored", Q(website_profile__qualification_score__isnull=True)),
    ]


class ContactSearchFilter(ChoiceFilter):
    title = "decision-maker search"
    parameter_name = "discovery"
    options = [
        ("todo", "Not searched yet", Q(website_profile__contact_discovery_status__isnull=True)),
        ("found", "Contacts found", Q(website_profile__contact_discovery_status="completed")),
        ("none", "No contacts found", Q(website_profile__contact_discovery_status="no_contacts")),
    ]


class ContactsFilter(ChoiceFilter):
    title = "contacts"
    parameter_name = "contacts"
    options = [("yes", "Has contacts", Q(contacts__isnull=False)), ("no", "No contacts", Q(contacts__isnull=True))]


class ProspectsFilter(ChoiceFilter):
    title = "prospects"
    parameter_name = "prospects"
    options = [
        ("ready", "Has a ready prospect", Q(prospects__outreach_status="ready", prospects__do_not_contact=False)),
        ("review", "Has prospects needing review", Q(prospects__outreach_status="needs_review")),
        ("contacted", "Contacted", Q(prospects__outreach_status="contacted")),
        ("none", "No prospects", Q(prospects__isnull=True)),
    ]


class RepliedFilter(ChoiceFilter):
    title = "replied"
    parameter_name = "replied"
    options = [("yes", "Replied", Q(emails__status="replied")), ("no", "Not replied", ~Q(emails__status="replied"))]


class EmailsFilter(ChoiceFilter):
    title = "emails"
    parameter_name = "emails"
    options = [
        ("review", "Email in review", Q(emails__status="in_review")),
        ("approved", "Email approved", Q(emails__status="approved")),
        ("sent", "Email sent", Q(emails__status__in=["sent", "opened"])),
        ("opened", "Email opened", Q(emails__status="opened")),
        ("none", "No email yet", Q(emails__isnull=True)),
    ]


class RatingFilter(ChoiceFilter):
    title = "Google rating"
    parameter_name = "rating"
    options = [
        ("45", "4.5 and above", Q(google_rating__gte=4.5)),
        ("40", "4.0 to 4.4", Q(google_rating__gte=4.0, google_rating__lt=4.5)),
        ("low", "Below 4.0", Q(google_rating__lt=4.0)),
        ("none", "No rating", Q(google_rating__isnull=True)),
    ]


class ReviewCountFilter(ChoiceFilter):
    title = "Google reviews"
    parameter_name = "reviews"
    options = [
        ("100", "100 or more", Q(google_review_count__gte=100)),
        ("20", "20 to 99", Q(google_review_count__gte=20, google_review_count__lt=100)),
        ("few", "Fewer than 20", Q(google_review_count__lt=20)),
        ("none", "None", Q(google_review_count__isnull=True) | Q(google_review_count=0)),
    ]


@admin.register(Business)
class BusinessAdmin(PipelineAdmin):
    list_display = ["name", "category", "city", "google_rating", "google_review_count", "website",
                    "research_status", "score", "qualification", "discovery", "contact_count", "prospect_count"]
    list_filter = ["discovery_campaign", ReviewFilter, HasWebsiteFilter, ResearchStatusFilter, ScoreFilter, ContactSearchFilter,
                   ContactsFilter, ProspectsFilter, EmailsFilter, RepliedFilter, RatingFilter, ReviewCountFilter, "category",
                   "country", "city"]
    search_fields = ["name", "category", "city", "state", "address", "domain", "phone", "website_url"]
    change_list_template = "admin/pipeline/business/change_list.html"
    readonly_fields = ["created_at", "updated_at", "first_discovered_at", "last_discovered_at", "profile_link",
                       "opening_hours_display"]
    fieldsets = [
        (None, {"fields": ["discovery_campaign", "name", "category", "website_url", "domain", "phone", "profile_link",
                           "review_status"]}),
        ("Location", {"fields": ["address", "city", "state", "country", "postal_code", "latitude", "longitude"]}),
        ("Google Maps", {"fields": ["google_place_id", "google_maps_url", "google_rating", "google_review_count",
                                    "opening_hours_display", "source", "source_url"], "classes": ["collapse"]}),
        ("Timestamps", {"fields": ["first_discovered_at", "last_discovered_at", "created_at", "updated_at"],
                        "classes": ["collapse"]}),
    ]
    inlines = [BusinessContactInline, ProspectInline, EmailInline, BusinessSourceInline]
    actions = ["approve_businesses", "reject_businesses", "refresh_details", "research_websites", "find_stakeholders",
               "verify_emails", "generate_emails", "mark_replied"]
    list_select_related = ["website_profile", "discovery_campaign"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            contact_total=Count("contacts", distinct=True), prospect_total=Count("prospects", distinct=True),
            profile_score=F("website_profile__qualification_score"),
        )

    # (label, query string, condition) for the quick-filter boxes above the list
    QUICK_FILTERS = [
        ("All businesses", "", Q()),
        ("Not researched", "research=none", ResearchStatusFilter.options[0][2]),
        ("Needs your review", "qualification=review", qualify.NEEDS_REVIEW),
        ("Qualified", "qualification=qualified", qualify.QUALIFIED),
        ("Score 80+", "score=80", ScoreFilter.options[0][2]),
        ("100+ reviews", "reviews=100", ReviewCountFilter.options[0][2]),
        ("Rated 4.5+", "rating=45", RatingFilter.options[0][2]),
        ("No contacts yet", "contacts=no", ContactsFilter.options[1][2]),
        ("Ready to email", "prospects=ready", ProspectsFilter.options[0][2]),
        ("Email sent", "emails=sent", EmailsFilter.options[2][2]),
    ]

    def changelist_view(self, request, extra_context=None):
        current = request.GET.urlencode()
        chips = [{"label": label, "query": query, "active": query == current,
                  "count": Business.objects.filter(condition).distinct().count()}
                 for label, query, condition in self.QUICK_FILTERS]
        return super().changelist_view(request, {**(extra_context or {}), "quick_filters": chips})

    @admin.display(description="Website")
    def website(self, obj):
        if not obj.website_url:
            return "-"
        return external_link(obj.website_url, Truncator(obj.domain or obj.website_url).chars(30))

    def _profile(self, obj):
        try:
            return obj.website_profile
        except BusinessWebsiteProfile.DoesNotExist:
            return None

    @admin.display(description="Research", ordering="website_profile__status")
    def research_status(self, obj):
        profile = self._profile(obj)
        return badge(profile.status, profile.get_status_display()) if profile else "-"

    @admin.display(description="Score", ordering="profile_score")
    def score(self, obj):
        return obj.profile_score if obj.profile_score is not None else "-"

    @admin.display(description="Qualification")
    def qualification(self, obj):
        if obj.review_status:
            return badge(obj.review_status, f"{obj.get_review_status_display()} by a person")
        profile = self._profile(obj)
        minimum = obj.discovery_campaign.min_score
        if profile and profile.status == "completed" and profile.qualification_score is not None:
            return badge("completed", "Qualified") if profile.qualification_score >= minimum else \
                badge("needs_review", f"Below {minimum}: review")
        if (profile and profile.status == "failed") or not obj.website_url:
            return badge("needs_review", "Not researchable: review")
        return "-"

    @admin.action(description="Approve: find decision makers for these businesses")
    def approve_businesses(self, request, queryset):
        count = queryset.update(review_status="approved")
        self.message_user(request, f"{count} business(es) approved; Find decision makers will include them.")

    @admin.action(description="Reject: never find decision makers for these businesses")
    def reject_businesses(self, request, queryset):
        count = queryset.update(review_status="rejected")
        self.message_user(request, f"{count} business(es) rejected.")

    @admin.display(description="Contacts search", ordering="website_profile__contact_discovery_status")
    def discovery(self, obj):
        profile = self._profile(obj)
        return profile.get_contact_discovery_status_display() if profile and profile.contact_discovery_status else "-"

    @admin.display(description="Contacts", ordering="contact_total")
    def contact_count(self, obj):
        return changelist_link(BusinessContact, obj.contact_total, business__id__exact=obj.pk)

    @admin.display(description="Prospects", ordering="prospect_total")
    def prospect_count(self, obj):
        return changelist_link(Prospect, obj.prospect_total, business__id__exact=obj.pk)

    @admin.display(description="Website research")
    def profile_link(self, obj):
        profile = self._profile(obj) if obj.pk else None
        if profile is None:
            return "Not researched yet"
        score = f", score {profile.qualification_score}" if profile.qualification_score is not None else ""
        return admin_link(profile, f"{profile.get_status_display()}{score}")

    @admin.display(description="Opening hours")
    def opening_hours_display(self, obj):
        return pretty_json(obj.opening_hours)

    @admin.action(description="Mark replied (the latest sent email of each business; that prospect gets no more)")
    def mark_replied(self, request, queryset):
        done = 0
        for business in queryset:
            try:
                review.mark_business_replied(business)
                done += 1
            except review.ReviewError:
                pass
        self.message_user(request, f"{done} business(es) marked replied (those with a sent email).")

    @admin.action(description="Refresh Google Maps details (category, reviews, city)")
    def refresh_details(self, request, queryset):
        return run_step("refresh_businesses", ids_arguments("--business-id", queryset.values_list("pk", flat=True)))

    @admin.action(description="Research websites (Module 2)")
    def research_websites(self, request, queryset):
        # Selected explicitly, so research them even if they were researched before.
        return run_step("research_websites", ["--redo", *ids_arguments("--business-id", queryset.values_list("pk", flat=True))])

    @admin.action(description="Find decision makers (Module 3)")
    def find_stakeholders(self, request, queryset):
        return run_step("find_stakeholders", ["--redo", *ids_arguments("--business-id", queryset.values_list("pk", flat=True))])

    @admin.action(description="Verify contact emails (Module 4)")
    def verify_emails(self, request, queryset):
        return run_step("verify_emails", ids_arguments("--business-id", queryset.values_list("pk", flat=True)))

    @admin.action(description="Generate outreach emails (Module 5)")
    def generate_emails(self, request, queryset):
        return HttpResponseRedirect(generate_url("business", queryset.values_list("pk", flat=True)))


@admin.register(BusinessSource)
class BusinessSourceAdmin(PipelineAdmin):
    list_display = ["business", "source", "source_business_id", "discovered_at"]
    list_filter = ["source"]
    search_fields = ["business__name", "source_business_id"]
    readonly_fields = ["business", "source", "source_business_id", "source_url", "discovered_at", "raw_data_display"]
    exclude = ["raw_data"]
    list_select_related = ["business"]

    @admin.display(description="Raw data")
    def raw_data_display(self, obj):
        return pretty_json(obj.raw_data)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------- Module 2: website research


@admin.register(BusinessWebsiteProfile)
class BusinessWebsiteProfileAdmin(PipelineAdmin):
    list_display = ["business", "status_badge", "qualification_score", "pages_scraped", "contact_discovery_status",
                    "updated_at"]
    list_filter = ["status", "contact_discovery_status"]
    search_fields = ["business__name", "business__domain"]
    list_select_related = ["business"]
    readonly_fields = ["business", "pages_scraped", "created_at", "updated_at", "contact_discovery_at",
                       "reasons_display", "findings_display", "emails_display", "urls_display", "links_display"]
    fieldsets = [
        (None, {"fields": ["business", "status", "qualification_score", "reasons_display", "agent_reasoning"]}),
        ("What the agent read", {"fields": ["pages_scraped", "urls_display", "findings_display", "emails_display",
                                            "links_display"]}),
        ("Contact discovery (Module 3)", {"fields": ["contact_discovery_status", "contact_discovery_at"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]
    actions = ["research_again", "rediscover_contacts"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Qualification reasons")
    def reasons_display(self, obj):
        return format_html("<ul>{}</ul>", format_html_join("", "<li>{}</li>", ((r,) for r in obj.qualification_reasons or []))) \
            if obj.qualification_reasons else "-"

    @admin.display(description="Findings per page")
    def findings_display(self, obj):
        rows = []
        for page in obj.scraped_pages or []:
            findings = format_html_join("", "<li>{}</li>", ((f,) for f in page.get("relevant_findings") or []))
            rows.append(format_html("<p><b>{}</b><br>{}</p><ul>{}</ul>", page.get("url", ""), page.get("summary", ""), findings))
        return format_html_join("", "{}", ((row,) for row in rows)) if rows else "-"

    @admin.display(description="Emails found")
    def emails_display(self, obj):
        return ", ".join(obj.emails or []) or "-"

    @admin.display(description="Pages read")
    def urls_display(self, obj):
        return format_html_join("", "<div>{}</div>", ((external_link(url),) for url in obj.scraped_urls or [])) \
            if obj.scraped_urls else "-"

    @admin.display(description="Links discovered")
    def links_display(self, obj):
        return pretty_json(obj.discovered_links)

    @admin.action(description="Research again (Module 2)")
    def research_again(self, request, queryset):
        return run_step("research_websites", ["--redo", *ids_arguments(
            "--business-id", queryset.values_list("business_id", flat=True))])

    @admin.action(description="Find decision makers again (Module 3)")
    def rediscover_contacts(self, request, queryset):
        return run_step("find_stakeholders", ["--redo", "--min-score", "0", *ids_arguments(
            "--business-id", queryset.values_list("business_id", flat=True))])


# ---------------------------------------------------------------- Module 3: contacts


@admin.register(BusinessContact)
class BusinessContactAdmin(PipelineAdmin):
    list_display = ["name", "business", "job_title", "email", "email_source", "status_badge", "is_primary",
                    "confidence", "candidate_count"]
    list_filter = ["email_status", "is_primary", "role_type", "email_source"]
    search_fields = ["name", "email", "business__name", "job_title"]
    list_select_related = ["business"]
    autocomplete_fields = ["business"]
    readonly_fields = ["created_at", "updated_at", "email_checked_at", "candidates_display", "check_details_display",
                       "source_urls_display"]
    fieldsets = [
        (None, {"fields": ["business", "name", "first_name", "last_name", "job_title", "role_type", "is_primary",
                           "linkedin_url"]}),
        ("Email", {"fields": ["email", "email_source", "email_status", "email_checked_at", "candidates_display",
                              "check_details_display"]}),
        ("How they were found", {"fields": ["confidence", "discovery_reasoning", "source_urls_display"]}),
        ("Raw data", {"fields": ["candidate_emails", "source_urls"], "classes": ["collapse"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]
    actions = ["verify_emails"]

    def save_model(self, request, obj, form, change):
        if not change and not obj.email_source:
            obj.email_source = "manual"  # kept when contact discovery runs again
        super().save_model(request, obj, form, change)

    @admin.display(description="Email status", ordering="email_status")
    def status_badge(self, obj):
        return badge(obj.email_status, obj.get_email_status_display() if obj.email_status else None)

    @admin.display(description="Candidates")
    def candidate_count(self, obj):
        return len(obj.candidate_emails or [])

    @admin.display(description="Candidate emails")
    def candidates_display(self, obj):
        rows = [(item.get("email", ""), item.get("pattern", ""), item.get("confidence", ""), item.get("check") or "not checked")
                for item in obj.candidate_emails or [] if isinstance(item, dict)]
        if not rows:
            return "-"
        body = format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>", rows)
        return format_html('<table class="pipeline-table"><tr><th>Email</th><th>Pattern</th><th>Confidence</th>'
                           "<th>Reacher</th></tr>{}</table>", body)

    @admin.display(description="Check details")
    def check_details_display(self, obj):
        return pretty_json(obj.email_check_details)

    @admin.display(description="Sources")
    def source_urls_display(self, obj):
        return format_html_join("", "<div>{}</div>", ((external_link(url),) for url in obj.source_urls or [])) \
            if obj.source_urls else "-"

    @admin.action(description="Verify these contacts' emails (Module 4)")
    def verify_emails(self, request, queryset):
        return run_step("verify_emails", ["--recheck", *ids_arguments("--contact-id", queryset.values_list("pk", flat=True))])


# ---------------------------------------------------------------- Module 4: prospects


GENERATE_SCOPES = {"business": ("--business-id", Business, "business(es)"),
                   "prospect": ("--prospect-id", Prospect, "prospect(s)")}


class GenerateEmailsForm(forms.Form):
    email_prompt = forms.ModelChoiceField(
        EmailPrompt.objects.filter(kind="initial"), required=False,
        empty_label="Each campaign's initial-email prompts (A/B split)",
        help_text="Leave empty to use each campaign's initial-email prompts. Use + to write a new prompt, the pencil "
                  "to edit one.",
    )
    regenerate = forms.BooleanField(required=False, label="Rewrite emails that are in review or rejected",
                                    help_text="Approved and sent emails are never changed.")
    limit = forms.IntegerField(required=False, min_value=1, help_text="At most this many prospects (optional).")
    dry_run = forms.BooleanField(required=False, label="Dry run",
                                 help_text="Show the emails in the run output without saving them.")


@admin.register(Prospect)
class ProspectAdmin(PipelineAdmin):
    list_display = ["email", "business", "contact", "status_badge", "verdict_badge", "outreach_badge",
                    "initial_email_prompt", "do_not_contact", "in_ghl", "qualification_score", "email_verified_at",
                    "last_contacted_at"]
    list_filter = ["email_status", "verdict", "outreach_status", "initial_email_prompt", "approved_by_person",
                   "do_not_contact", "email_verification_provider"]
    search_fields = ["email", "business__name", "contact__name"]
    list_select_related = ["business", "contact", "initial_email_prompt"]
    autocomplete_fields = ["business", "contact"]
    readonly_fields = ["email_verified_at", "email_verification_provider", "last_contacted_at", "created_at",
                       "updated_at", "emails_link", "verification_details_display", "ghl_contact_id",
                       "sequence_stopped_at", "sequence_stop_reason", "initial_email_prompt"]
    fieldsets = [
        (None, {"fields": ["business", "contact", "email", "do_not_contact"]}),
        ("Verification (Module 4)", {"fields": ["email_status", "verdict", "verification_note",
                                                "verification_details_display", "email_verification_provider",
                                                "email_verified_at", "qualification_score"]}),
        ("Outreach", {"fields": ["outreach_status", "approved_by_person", "initial_email_prompt", "outreach_priority",
                                 "outreach_facts", "research_summary", "last_contacted_at", "emails_link"]}),
        ("GoHighLevel", {"fields": ["ghl_contact_id", "sequence_stopped_at", "sequence_stop_reason"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]
    actions = ["approve_prospects", "reject_prospects", "generate_emails", "regenerate_emails", "mark_replied",
               "mark_do_not_contact", "clear_do_not_contact"]

    @admin.display(description="In GHL", boolean=True)
    def in_ghl(self, obj):
        return bool(obj.ghl_contact_id)

    @admin.action(description="Approve: email these addresses although verification did not confirm them")
    def approve_prospects(self, request, queryset):
        count = queryset.filter(outreach_status__in=["needs_review", "pending", "rejected"]).exclude(
            email_status="undeliverable").update(outreach_status="ready", approved_by_person=True)
        self.message_user(request, f"{count} prospect(s) approved; they get emails and become GoHighLevel contacts. "
                                   "Undeliverable addresses are never approved.")

    @admin.action(description="Reject: never email these addresses")
    def reject_prospects(self, request, queryset):
        count = queryset.exclude(outreach_status="contacted").update(outreach_status="rejected",
                                                                     approved_by_person=False)
        self.message_user(request, f"{count} prospect(s) rejected (already contacted ones are left as they are).")

    @admin.display(description="Email", ordering="email_status")
    def status_badge(self, obj):
        return badge(obj.email_status, obj.get_email_status_display() if obj.email_status else None)

    @admin.display(description="Verdict", ordering="verdict")
    def verdict_badge(self, obj):
        if not obj.verdict:
            return "-"
        return format_html('<span class="pipeline-badge pipeline-{}" title="{}">{}</span>', obj.email_status or "none",
                           obj.verification_note or "", obj.get_verdict_display())

    @admin.display(description="Reacher findings")
    def verification_details_display(self, obj):
        return pretty_json(obj.verification_details)

    @admin.display(description="Outreach", ordering="outreach_status")
    def outreach_badge(self, obj):
        return badge(obj.outreach_status, obj.get_outreach_status_display())

    @admin.display(description="Emails")
    def emails_link(self, obj):
        return changelist_link(Email, f"{obj.emails.count()} email(s)", prospect__id__exact=obj.pk) if obj.pk else "-"

    @admin.action(description="Generate outreach emails (Module 5)")
    def generate_emails(self, request, queryset):
        return HttpResponseRedirect(generate_url("prospect", queryset.values_list("pk", flat=True)))

    @admin.action(description="Regenerate emails still in review or rejected (Module 5)")
    def regenerate_emails(self, request, queryset):
        return HttpResponseRedirect(generate_url("prospect", queryset.values_list("pk", flat=True), regenerate=True))

    # ------------------------------------------------------------ "Generate emails" page

    def get_urls(self):
        return [path("generate/", self.admin_site.admin_view(self.generate_view), name="pipeline_prospect_generate")] \
            + super().get_urls()

    def generate_view(self, request):
        source = request.POST if request.method == "POST" else request.GET
        scope = source.get("scope") if source.get("scope") in GENERATE_SCOPES else None
        ids = [int(pk) for pk in (source.get("ids") or "").split(",") if pk.strip().isdigit()] if scope else []
        form = GenerateEmailsForm(request.POST or None, initial={"regenerate": request.GET.get("regenerate") == "1"})
        form.fields["email_prompt"].widget = RelatedFieldWidgetWrapper(
            form.fields["email_prompt"].widget, Email._meta.get_field("email_prompt").remote_field, self.admin_site,
            can_add_related=True, can_change_related=True, can_view_related=True,
        )
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            arguments = ids_arguments(GENERATE_SCOPES[scope][0], ids) if scope else []
            if data["email_prompt"]:
                arguments += ["--email-prompt-id", str(data["email_prompt"].pk)]
            if data["regenerate"]:
                arguments.append("--regenerate")
            if data["limit"]:
                arguments += ["--limit", str(data["limit"])]
            if data["dry_run"]:
                arguments.append("--dry-run")
            run = jobs.start_run("generate_emails", arguments, request.user)
            self.message_user(request, f"Started: {run.command_line}", messages.SUCCESS)
            return HttpResponseRedirect(admin_url(run))
        if scope:
            model, label = GENERATE_SCOPES[scope][1], GENERATE_SCOPES[scope][2]
            names = [str(obj) for obj in model.objects.filter(pk__in=ids)[:10]]
            target = f"{len(ids)} selected {label}: " + ", ".join(names) + ("..." if len(ids) > 10 else "")
        else:
            target = "every ready prospect that has no email yet"
        context = {
            **self.admin_site.each_context(request),
            "title": "Generate emails",
            "form": form,
            "media": self.media + form.media,
            "target": target,
            "scope": scope or "",
            "ids": ",".join(map(str, ids)),
            "opts": self.model._meta,
            "prompts": EmailPrompt.objects.filter(kind="initial"),
        }
        return TemplateResponse(request, "admin/pipeline/generate_emails.html", context)

    @admin.action(description="Mark replied (no more emails to these prospects)")
    def mark_replied(self, request, queryset):
        done = 0
        for prospect in queryset.select_related("business"):
            try:
                review.mark_business_replied(prospect.business, prospect_id=prospect.pk)
                done += 1
            except review.ReviewError:
                pass
        self.message_user(request, f"{done} prospect(s) marked replied; they get no more emails.")

    @admin.action(description="Mark do not contact")
    def mark_do_not_contact(self, request, queryset):
        count = queryset.update(do_not_contact=True)
        self.message_user(request, f"{count} prospect(s) marked do not contact; they will never be emailed.")

    @admin.action(description="Clear do not contact")
    def clear_do_not_contact(self, request, queryset):
        count = queryset.update(do_not_contact=False)
        self.message_user(request, f"{count} prospect(s) can be contacted again.")


# ---------------------------------------------------------------- Module 5: emails and opens


class EmailReviewForm(forms.ModelForm):
    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 16, "class": "vLargeTextField"}),
        max_length=review.MAX_BODY,
        help_text="The Axioware footer and the tracking image are added automatically below the body. "
                  "Saving sends the email back to review.",
    )

    class Meta:
        model = Email
        fields = ["subject"]
        widgets = {"subject": forms.TextInput(attrs={"class": "vLargeTextField", "maxlength": review.MAX_SUBJECT})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["body"].initial = review.editable_body(self.instance)

    def clean_subject(self):
        subject = " ".join((self.cleaned_data.get("subject") or "").split())
        if len(subject) > review.MAX_SUBJECT:
            raise forms.ValidationError(f"Keep the subject under {review.MAX_SUBJECT} characters.")
        return subject


class OpenEventInline(ReadOnlyInline):
    model = EmailOpenEvent
    fields = ["opened_at", "user_agent"]
    readonly_fields = fields
    show_change_link = False
    max_num = 0


@admin.register(Email)
class EmailAdmin(PipelineAdmin):
    form = EmailReviewForm
    list_display = ["id", "business", "recipient", "subject_short", "step", "email_prompt", "status_badge",
                    "scheduled_at", "sent_at", "edited"]
    list_display_links = ["id", "subject_short"]
    list_filter = ["status", "sequence_step", "email_prompt", "generation_provider"]
    search_fields = ["business__name", "recipient", "subject"]
    list_select_related = ["business", "prospect", "email_prompt"]
    actions = ["approve_selected", "hand_over_selected", "reject_selected", "mark_replied_selected"]
    inlines = [OpenEventInline]
    change_form_template = "admin/pipeline/email/change_form.html"
    change_list_template = "admin/pipeline/email/change_list.html"
    details = ["status_badge", "recipient", "business_link", "contact_link", "prospect_link", "written_by", "email_prompt",
               "generated_at", "reviewed_at", "review_note", "edited_at"]
    delivery = ["scheduled_at", "released_at", "ghl_error", "sent_at", "sent_from", "message_id", "send_error", "open_count", "first_opened_at", "last_opened_at",
                "tracking_token"]

    def has_add_permission(self, request):
        return False  # emails are written by generate_emails

    def editable(self, obj) -> bool:
        return obj is not None and obj.status in review.EDITABLE

    def get_fieldsets(self, request, obj=None):
        content = ["subject", "body"] if self.editable(obj) else ["subject", "body_display"]
        return [
            ("Content", {"fields": content}),
            ("Review", {"fields": self.details}),
            ("Delivery and opens", {"fields": self.delivery, "classes": ["collapse"]}),
        ]

    def get_readonly_fields(self, request, obj=None):
        fields = [*self.details, *self.delivery, "body_display"]
        return fields if self.editable(obj) else [*fields, "subject"]

    def get_form(self, request, obj=None, change=False, **kwargs):
        if not self.editable(obj):
            kwargs["form"] = forms.ModelForm
        return super().get_form(request, obj, change=change, **kwargs)

    def save_model(self, request, obj, form, change):
        if not form.has_changed() or "body" not in form.cleaned_data:
            return
        original = Email.objects.get(pk=obj.pk)
        try:
            message = review.save_edit(original, form.cleaned_data["subject"], form.cleaned_data["body"])
        except review.ReviewError as error:
            self.message_user(request, str(error), messages.ERROR)
        else:
            self.message_user(request, message)

    def message_user(self, request, message, level=messages.INFO, *args, **kwargs):
        # The edit outcome is reported by save_model; skip Django's generic "changed successfully" notice.
        if "was changed successfully" in str(message):
            return
        super().message_user(request, message, level, *args, **kwargs)

    @admin.display(description="Subject", ordering="subject")
    def subject_short(self, obj):
        return Truncator(obj.subject).chars(70)

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Edited", boolean=True)
    def edited(self, obj):
        return obj.edited_at is not None

    @admin.display(description="Body")
    def body_display(self, obj):
        return format_html('<pre class="pipeline-pre">{}</pre>', review.editable_body(obj))

    @admin.display(description="Business")
    def business_link(self, obj):
        return admin_link(obj.business)

    @admin.display(description="Contact")
    def contact_link(self, obj):
        contact = obj.contact
        title = f" ({contact.job_title})" if contact.job_title else ""
        return admin_link(contact, f"{contact}{title}")

    @admin.display(description="Prospect")
    def prospect_link(self, obj):
        prospect = obj.prospect
        state = "ready" if prospect.is_ready else "NOT ready for outreach"
        return format_html("{} - {}", admin_link(prospect), state)

    @admin.display(description="Written by")
    def written_by(self, obj):
        return f"{obj.generation_provider} {obj.generation_model}" if obj.generation_model else "-"

    # ------------------------------------------------------------ list page

    def changelist_view(self, request, extra_context=None):
        counts = review.status_counts()
        chips = [{"status": status, "label": Email.Status(status).label, "count": counts[status]}
                 for status in review.STATUS_ORDER]
        extra_context = {**(extra_context or {}), "status_chips": chips, "email_total": sum(counts.values()),
                         "open_rate": review.open_rate(counts), "open_tracking": generation.OPEN_TRACKING}
        return super().changelist_view(request, extra_context=extra_context)

    @admin.display(description="Step", ordering="sequence_step")
    def step(self, obj):
        return "First email" if obj.sequence_step == generation.SEQUENCE_STEP else f"Follow-up {obj.sequence_step - 1}"

    @admin.action(description="Mark replied (no more emails to their prospects)")
    def mark_replied_selected(self, request, queryset):
        done = skipped = 0
        for email in queryset:
            try:
                review.mark_replied(email)
                done += 1
            except review.ReviewError:
                skipped += 1
        extra = f", {skipped} skipped (not sent yet)" if skipped else ""
        self.message_user(request, f"{done} email(s) marked replied{extra}.")

    @admin.action(description="Hand approved emails to GoHighLevel (those whose hand-over failed)")
    def hand_over_selected(self, request, queryset):
        done, errors = 0, []
        for email in queryset.filter(status="approved"):
            try:
                review.hand_over(email)
                done += 1
            except review.ReviewError as error:
                errors.append(f"#{email.pk}: {error}")
        self.message_user(request, f"{done} email(s) handed to GoHighLevel." + (" " + " ".join(errors[:5]) if errors else ""),
                          messages.WARNING if errors else messages.SUCCESS)

    @admin.action(description="Approve selected emails (in review only)")
    def approve_selected(self, request, queryset):
        done, skipped = review.bulk("approve", list(queryset.values_list("pk", flat=True)))
        extra = f", {skipped} skipped (wrong status or placeholder text)" if skipped else ""
        self.message_user(request, f"{done} email(s) approved{extra}.")

    @admin.action(description="Reject selected emails")
    def reject_selected(self, request, queryset):
        done, skipped = review.bulk("reject", list(queryset.values_list("pk", flat=True)))
        extra = f", {skipped} skipped (wrong status)" if skipped else ""
        self.message_user(request, f"{done} email(s) rejected{extra}.")

    # ------------------------------------------------------------ review page

    def get_urls(self):
        actions = [
            path(f"<int:object_id>/{name}/", self.admin_site.admin_view(view), name=f"pipeline_email_{name}")
            for name, view in (("approve", self.approve_view), ("reject", self.reject_view),
                               ("reopen", self.reopen_view), ("regenerate", self.regenerate_view),
                               ("replied", self.replied_view), ("handover", self.handover_view))
        ]
        return actions + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        email = Email.objects.select_related("prospect", "business", "contact", "business__website_profile").filter(
            pk=object_id).first()
        if email is not None:
            try:
                profile = email.business.website_profile
            except BusinessWebsiteProfile.DoesNotExist:
                profile = None
            extra_context = {
                **(extra_context or {}),
                "email": email,
                "preview": review.preview_html(email),
                "findings": generation.website_findings(profile.scraped_pages if profile else []),
                "qualification_reasons": (profile.qualification_reasons if profile else None) or [],
                "qualification_score": profile.qualification_score if profile else None,
                "prospect_ready": review.prospect_ready(email),
                "website_link": safe_url(email.business.website_url),
                "has_placeholder": review.has_placeholder(email),
                "in_review_count": Email.objects.filter(status="in_review").count(),
                "next_review": review.next_in_review(email.pk),
                "email_prompts": EmailPrompt.objects.filter(
                    kind="initial" if email.sequence_step == generation.SEQUENCE_STEP else "follow_up"),
            }
        return super().change_view(request, object_id, form_url, extra_context)

    def _review_action(self, request, object_id, action, advance=False):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        email = get_object_or_404(Email.objects.select_related("prospect"), pk=object_id)
        if not self.has_change_permission(request, email):
            return HttpResponseRedirect(reverse("admin:index"))
        try:
            message = action(email)
        except review.ReviewError as error:
            self.message_user(request, str(error), messages.ERROR)
            return HttpResponseRedirect(admin_url(email))
        following = review.next_in_review(email.pk) if advance else None
        if following:
            self.message_user(request, f"Email {email.pk}: {message} Next in review:", messages.SUCCESS)
            return HttpResponseRedirect(reverse("admin:pipeline_email_change", args=[following]))
        self.message_user(request, message, messages.SUCCESS)
        return HttpResponseRedirect(admin_url(email))

    def approve_view(self, request, object_id):
        return self._review_action(request, object_id, review.approve, advance=not request.POST.get("stay"))

    def reject_view(self, request, object_id):
        return self._review_action(request, object_id, lambda e: review.reject(e, request.POST.get("note", "")), advance=True)

    def reopen_view(self, request, object_id):
        return self._review_action(request, object_id, review.reopen)

    def regenerate_view(self, request, object_id):
        prompt_id = request.POST.get("email_prompt") or ""
        prompt = EmailPrompt.objects.filter(pk=prompt_id).first() if prompt_id.isdigit() else None
        return self._review_action(request, object_id, lambda e: review.regenerate(e, prompt))

    def replied_view(self, request, object_id):
        return self._review_action(request, object_id, review.mark_replied)

    def handover_view(self, request, object_id):
        return self._review_action(request, object_id, review.hand_over)


@admin.register(EmailOpenEvent)
class EmailOpenEventAdmin(PipelineAdmin):
    list_display = ["email", "opened_at", "user_agent_short"]
    search_fields = ["email__recipient", "user_agent"]
    list_select_related = ["email"]
    date_hierarchy = "opened_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="User agent")
    def user_agent_short(self, obj):
        return Truncator(obj.user_agent or "").chars(90)


# ---------------------------------------------------------------- pipeline runs


class PipelineRunForm(forms.ModelForm):
    command = forms.ChoiceField(choices=list(jobs.COMMANDS.items()))
    arguments_text = forms.CharField(
        label="Arguments", required=False, widget=forms.TextInput(attrs={"class": "vLargeTextField"}),
        help_text='Exactly as on the command line, e.g. --limit 5 --dry-run. See the options below.',
    )

    class Meta:
        model = PipelineRun
        fields = ["command"]

    def clean(self):
        cleaned = super().clean()
        command = cleaned.get("command")
        if command:
            try:
                cleaned["arguments"] = jobs.build_arguments(command, arguments=cleaned.get("arguments_text") or "")
            except ValueError as error:
                raise forms.ValidationError(str(error)) from error
        return cleaned


@admin.register(PipelineRun)
class PipelineRunAdmin(PipelineAdmin):
    form = PipelineRunForm
    list_display = ["id", "command", "arguments_short", "status_badge", "created_by", "created_at", "duration"]
    list_filter = ["status", "command"]
    search_fields = ["output", "command"]
    change_form_template = "admin/pipeline/pipelinerun/change_form.html"
    readonly = ["command_line", "status_badge", "exit_code", "created_by", "created_at", "started_at",
                "finished_at", "duration", "output_display"]

    def has_change_permission(self, request, obj=None):
        return False  # a run is a record of what happened: view it, stop it, run it again or delete it

    def get_fields(self, request, obj=None):
        return ["command", "arguments_text"] if obj is None else self.readonly

    def get_readonly_fields(self, request, obj=None):
        return [] if obj is None else self.readonly

    def get_changeform_initial_data(self, request):
        return {"command": request.GET.get("command", ""), "arguments_text": request.GET.get("arguments", "")}

    def save_model(self, request, obj, form, change):
        if change:
            return
        obj.arguments = form.cleaned_data["arguments"]
        obj.created_by = request.user
        super().save_model(request, obj, form, change)
        transaction.on_commit(lambda: jobs.launch(obj.pk))

    def response_add(self, request, obj, post_url_continue=None):
        self.message_user(request, f"Started: {obj.command_line}", messages.SUCCESS)
        return HttpResponseRedirect(admin_url(obj))

    def changelist_view(self, request, extra_context=None):
        jobs.refresh_stale_runs()
        return super().changelist_view(request, extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        extra_context = {**(extra_context or {}), "commands": [jobs.describe_command(c) for c in jobs.COMMANDS]}
        return super().add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        jobs.refresh_stale_runs()
        run = PipelineRun.objects.filter(pk=object_id).first()
        if run is not None:
            extra_context = {**(extra_context or {}), "run": run,
                             "rerun_url": run_form_url(run.command, run.arguments) if run.command in jobs.COMMANDS else None}
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        return [path("<int:object_id>/cancel/", self.admin_site.admin_view(self.cancel_view),
                     name="pipeline_pipelinerun_cancel")] + super().get_urls()

    def cancel_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        run = get_object_or_404(PipelineRun, pk=object_id)
        self.message_user(request, jobs.cancel_run(run))
        return HttpResponseRedirect(admin_url(run))

    @admin.display(description="Command")
    def command_line(self, obj):
        return format_html("<code>{}</code>", obj.command_line)

    @admin.display(description="Arguments")
    def arguments_short(self, obj):
        return Truncator(" ".join(obj.arguments)).chars(60) or "-"

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Duration")
    def duration(self, obj):
        if not obj.started_at:
            return "-"
        from django.utils import timezone

        seconds = int(((obj.finished_at or timezone.now()) - obj.started_at).total_seconds())
        return f"{seconds // 3600}h {seconds // 60 % 60}m {seconds % 60}s" if seconds >= 3600 else f"{seconds // 60}m {seconds % 60}s"

    @admin.display(description="Output")
    def output_display(self, obj):
        return format_html('<pre class="pipeline-pre pipeline-output">{}</pre>', obj.output or "(no output yet)")
